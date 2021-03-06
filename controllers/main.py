import logging
import traceback
from io import StringIO

from psycopg2 import OperationalError

import odoo
from odoo import http, tools
from odoo.service.model import PG_CONCURRENCY_ERRORS_TO_RETRY

from odoo.addons.odoo_magento2_ept.models.backend.session import ConnectorSessionHandler
from odoo.addons.odoo_magento2_ept.models.logs.job import (OpenERPJobStorage,
                         ENQUEUED)
from odoo.addons.odoo_magento2_ept.models.backend.exception import (NoSuchJobError,
                         NotReadableJobError,
                         RetryableJobError,
                         FailedJobError,
                         NothingToDoJob,
                         CanceledJobError)

_logger = logging.getLogger(__name__)

PG_RETRY = 5  # seconds


# TODO: perhaps the notion of ConnectionSession is less important
#       now that we are running jobs inside a normal Odoo worker


class RunJobController(http.Controller):

    job_storage_class = OpenERPJobStorage

    def _load_job(self, session, job_uuid):
        """ Reload a job from the backend """
        try:
            job = self.job_storage_class(session).load(job_uuid)
        except NoSuchJobError:
            # just skip it
            job = None
        except NotReadableJobError:
            _logger.exception('Could not read job: %s', job_uuid)
            raise
        return job

    def _try_perform_job(self, session_hdl, job):
        """Try to perform the job."""

        # if the job has been manually set to DONE or PENDING,
        # or if something tries to run a job that is not enqueued
        # before its execution, stop
        if job.state != ENQUEUED:
            _logger.warning('job %s is in state %s '
                            'instead of enqueued in /runjob',
                            job.uuid, job.state)
            return

        with session_hdl.session() as session:
            # TODO: set_started should be done atomically with
            #       update queue_job set=state=started
            #       where state=enqueid and id=
            job.set_started()
            self.job_storage_class(session).store(job)

        _logger.debug('%s started', job)
        with session_hdl.session() as session:
            job.perform(session)
            job.set_done()
            self.job_storage_class(session).store(job)
        _logger.debug('%s done', job)

    @http.route('/connector/runjob', type='http', auth='none')
    def runjob(self, db, job_uuid, **kw):

        session_hdl = ConnectorSessionHandler(db,
                                              odoo.SUPERUSER_ID)

        def retry_postpone(job, message, seconds=None):
            with session_hdl.session() as session:
                job.postpone(result=message, seconds=seconds)
                job.set_hold(reset_retry=False)
                self.job_storage_class(session).store(job)

        with session_hdl.session() as session:
            job = self._load_job(session, job_uuid)
            if job is None:
                return ""

        try:
            try:
                self._try_perform_job(session_hdl, job)
            except OperationalError as err:
                # Automatically retry the typical transaction serialization
                # errors
                if err.pgcode not in PG_CONCURRENCY_ERRORS_TO_RETRY:
                    raise

                retry_postpone(job, tools.ustr(err.pgerror, errors='replace'),
                               seconds=PG_RETRY)
                _logger.debug('%s OperationalError, postponed', job)

        except NothingToDoJob as err:
            if str(err):
                msg = str(err)
            else:
                msg = None
            job.cancel(msg)
            with session_hdl.session() as session:
                self.job_storage_class(session).store(job)

        except RetryableJobError as err:
            # delay the job later, requeue
            retry_postpone(job, str(err), seconds=err.seconds)
            _logger.debug('%s postponed', job)

        except FailedJobError as err:
            buff = StringIO()
            traceback.print_exc(file=buff)
            _logger.error(buff.getvalue())
            if str(err) :
                msg = str(err)
            else :
                msg = None
            job.set_failed(result=msg,exc_info=buff.getvalue())
            with session_hdl.session() as session:
                self.job_storage_class(session).store(job)
            raise
            
        return ""
