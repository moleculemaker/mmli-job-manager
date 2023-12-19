import json
import time
import threading

from kubernetes import watch, config as kubeconfig
from kubernetes.client.rest import ApiException
from requests import HTTPError

from dbconnector import DbConnector
from global_vars import config, log
import kubejob


# watched_namespaces = ["test"]
# for namespace in watched_namespaces:
#    count = 10
#    w = watch.Watch()
#    for event in w.stream(custom.list_namespaced_custom_object,
#                          group=CRD_GROUP_NAME,
#                          version=CRD_VERSION_V1,
#                          namespace=namespace,
#                          plural=CRD_USERAPPS_PLURAL,
#                          _request_timeout=60):
#        print("Event: %s %s" % (event['type'], event['object']['metadata']['name']))
#        count -= 1
#        time.sleep(10)
#        if not count:
#            w.stop()


class KubeEventWatcher:

    def __init__(self):
        self.logger = log
        #self.logger.setLevel('DEBUG')
        self.thread = threading.Thread(target=self.run, name='kube-event-watcher', daemon=True)
        # Get global instance of the job handler database interface
        self.db = DbConnector(
            mysql_host=config['db']['host'],
            mysql_user=config['db']['user'],
            mysql_password=config['db']['pass'],
            mysql_database=config['db']['database'],
        )

        self.stream = None
        self.logger.info('Starting KubeWatcher')
        self.thread.start()
        self.logger.info('Started KubeWatcher')

    def run(self):
        kubeconfig.load_incluster_config()

        # Ignore kube-system namespace
        # TODO: Parameterize this?
        ignored_namespaces = ['kube-system']
        self.logger.info('KubeWatcher watching all namespaces except for: ' + str(ignored_namespaces))

        # TODO: Parameterize this?
        required_labels = {
            'type': 'uws-job'
        }
        self.logger.info('KubeWatcher looking for required labels: ' + str(required_labels))

        timeout_seconds = 0
        k8s_event_stream = None

        w = watch.Watch()

        while True:
            time.sleep(1)
            self.logger.info('KubeWatcher is connecting...')
            try:
                # List all pods in watched namespace to get resource_version
                namespaced_jobs = kubejob.api_batch_v1.list_namespaced_job(namespace=kubejob.get_namespace())
                resource_version = namespaced_jobs['metadata']['resource_version'] if 'metadata' in namespaced_jobs and 'resource_version' in namespaced_jobs['metadata'] else ''

                # Then, watch for new events using the most recent resource_version
                # Resource version is used to keep track of stream progress (in case of resume/retry)
                k8s_event_stream = w.stream(func=kubejob.api_batch_v1.list_namespaced_job,
                                            namespace=kubejob.get_namespace(),
                                            resource_version=resource_version,
                                            timeout_seconds=timeout_seconds)

                self.logger.info('KubeWatcher connected!')

                # Parse events in the stream for Pod phase updates
                for event in k8s_event_stream:
                    resource_version = event['object'].metadata.resource_version

                    # Skip Pods in ignored namespaces
                    if event['object'].metadata.namespace in ignored_namespaces:
                        self.logger.debug('Skipping event in excluded namespace')
                        continue

                    # Examine labels, ignore if not uws-job
                    # self.logger.debug('Event recv\'d: %s' % event)
                    labels = event['object'].metadata.labels

                    if labels is None and len(required_labels) > 0:
                        self.logger.warning(
                            'WARNING: Skipping due to missing label(s): ' + str(required_labels))
                        continue

                    missing_labels = [x for x in required_labels if x not in labels]
                    if len(missing_labels) > 0:
                        self.logger.warning(
                            'WARNING: Skipping due to missing required label(s): ' + str(missing_labels))
                        continue

                    # TODO: lookup associated userapp using resource name
                    name = event['object'].metadata.name

                    # Parse name into userapp_id + ssid
                    segments = name.split('-')
                    if len(segments) < 3:
                        self.logger.warning('WARNING: Invalid number of segments -  JobName=%s' % name)
                        continue

                    # uws-job-jobid => we want last segment
                    job_id = segments[-1]

                    type = event['type']
                    status = event['object'].status
                    conditions = status.conditions

                    # Calculate new status
                    self.logger.debug(f'Event: job_id={job_id}   type={type}   status={status}')
                    new_phase = None
                    if conditions is None:
                        new_phase = 'executing'
                    elif len(conditions) > 0 and conditions[0].type == 'Complete':
                        new_phase = 'completed'
                    elif status.failed > 0:
                        new_phase = 'error'
                    else:
                        self.logger.info(f'>> Skipped job update: {job_id}-> {new_phase}')
                        self.logger.debug(f'>> Status: {str(status)}')

                    # Write status update back to database
                    if new_phase is not None:
                        self.logger.debug('Updating job phase: %s -> %s' % (job_id, new_phase))
                        self.db.update_job(
                            job_id=job_id,
                            phase=new_phase,
                        )
                        self.logger.info('Updated job phase: %s -> %s' % (job_id, new_phase))

            except (ApiException, HTTPError) as e:
                self.logger.error('HTTPError encountered - KubeWatcher reconnecting to Kube API: %s' % str(e))
                if k8s_event_stream:
                    k8s_event_stream.close()
                k8s_event_stream = None
                if e.status == 410:
                    # Resource too old
                    resource_version = None
                    self.logger.warning("Resource too old (410) - reconnecting: " + str(e))
                time.sleep(2)
                continue
            except Exception as e:
                self.logger.error('Unknown exception - KubeWatcher reconnecting to Kube API: %s' % str(e))
                if k8s_event_stream:
                    k8s_event_stream.close()
                k8s_event_stream = None
                time.sleep(2)
                continue

    def is_alive(self):
        return self.thread and self.thread.is_alive()

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        if self.is_alive():
            self.thread.join(timeout=3)
            self.thread = None

