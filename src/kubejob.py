import global_vars
from global_vars import config, log
from kubernetes import client, config as kubeconfig
from kubernetes.client.rest import ApiException
import os
import shutil
import yaml
import json
from jinja2 import Template
import uuid
import base64


## Load Kubernetes cluster config. Unhandled exception if not in Kubernetes environment.
try:
    kubeconfig.load_kube_config(config_file=config['server']['kubeconfig'])
except:
    kubeconfig.load_incluster_config()
configuration = client.Configuration()
api_batch_v1 = client.BatchV1Api(client.ApiClient(configuration))
api_v1 = client.CoreV1Api(client.ApiClient(configuration))

def get_namespace():
    # When running in a pod, the namespace should be determined automatically,
    # otherwise we assume the local development is in the default namespace
    try:
        with open('/var/run/secrets/kubernetes.io/serviceaccount/namespace', 'r') as file:
            namespace = file.read().replace('\n', '')
    except:
        try:
            namespace = config['server']['namespace']
        except:
            namespace = 'default'
    return namespace


def generate_uuid():
    return str(uuid.uuid4()).replace("-", "")


def get_job_name_from_id(job_id):
    return 'uws-job-{}'.format(job_id)


def get_job_root_dir_from_id(job_id):
    return os.path.join(config['uws']['workingVolume']['mountPath'], 'jobs', job_id)


def list_job_output_files(job_id):
    job_filepaths = []
    try:
        job_output_dir = os.path.join(get_job_root_dir_from_id(job_id), 'out')
        log.debug(f'Listing job files ({job_id}) [{job_output_dir}]:')
        if os.path.isdir(job_output_dir):
            for dirpath, dirnames, filenames in os.walk(job_output_dir):
                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    if not filename.startswith('.') and os.path.isfile(filepath):
                        job_filepaths.append(filepath)
                        log.debug(filepath)
    except Exception as e:
        log.error(str(e))
        raise e
    return job_filepaths


def list_jobs(job_id=None):
    jobs = []
    response = {
        'jobs': jobs,
        'status': global_vars.STATUS_OK,
        'message': '',
    }
    try:
        namespace = get_namespace()
        if job_id:
            api_response = api_batch_v1.list_namespaced_job(
                namespace=namespace,
                label_selector=f'jobId={job_id}',
            )
        else:
            api_response = api_batch_v1.list_namespaced_job(
                namespace=namespace,
                label_selector=f'type=uws-job',
            )
        ## ref: https://github.com/kubernetes-client/python/blob/549482d8842a47c8b51122422e879e7cc497bf88/kubernetes/docs/V1Job.md
        for item in api_response.items:
            envvars = []
            for envvar in item.spec.template.spec.containers[0].env:
                envvars.append({
                    'name': envvar.name,
                    'value': envvar.value,
                })
            command = []
            for command_element in item.spec.template.spec.containers[0].command:
                command.append(command_element.strip())
            job = {
                'name': item.metadata.name,
                ## CreationTimestamp is a timestamp representing the server time when this object was created.
                'creation_time': item.metadata.creation_timestamp,
                'job_id': item.metadata.labels['jobId'],
                'run_id': item.metadata.labels['runId'],
                'owner_id': item.metadata.labels['ownerId'],
                'command': command,
                'environment': envvars,
                'output_files': list_job_output_files(item.metadata.labels['jobId']),
                'status': {
                    ## active: The number of actively running pods.
                    'active': True if item.status.active else False,
                    ## start_time: Represents time when the job was acknowledged by the job controller. 
                    'start_time': item.status.start_time,
                    ## completion_time: Represents time when the job was completed.
                    'completion_time': item.status.completion_time,
                    ## succeeded: The number of pods which reached phase Succeeded.
                    'succeeded': True if item.status.succeeded else False,
                    ## failed: The number of pods which reached phase Failed.
                    'failed': True if item.status.failed else False,
                },
            }
            jobs.append(job)
        response['jobs'] = jobs
    except Exception as e:
        msg = str(e)
        log.error(msg)
        response['status'] = global_vars.STATUS_ERROR
        response['message'] = msg
    return response


def delete_job(job_id: str) -> None:
    namespace = get_namespace()
    job_name = get_job_name_from_id(job_id)
    # config_map_name = f'''{job_name}-positions'''
    body = client.V1DeleteOptions(propagation_policy='Background')
    api_response = None
    try:
        api_response = api_batch_v1.delete_namespaced_job(
            namespace=namespace,
            name=job_name,
            body=body,
        )
    except ApiException as e:
        ## No need to raise exception for 404 not found errors
        log.error(f'''Error deleting Kubernetes Job: {e}''')
        if api_response:
            log.error(json.dumps(api_response))
    except Exception as e:
        log.error(f'''Error deleting Kubernetes Job: {e}''')
        raise
    # try:
    #     api_response = api_v1.delete_namespaced_config_map(
    #         namespace=namespace,
    #         name=config_map_name,
    #         body=body,
    #     )
    # except ApiException as e:
    #     ## No need to raise exception for 404 not found errors
    #     log.error(f'''Error deleting Kubernetes ConfigMap: {e}''')
    #     if api_response:
    #         log.error(json.dumps(api_response))
    # except Exception as e:
    #     log.error(f'''Error deleting Kubernetes ConfigMap: {e}''')
    #     raise


def create_job(image_repo, command, job_id=None, run_id=None, owner_id=None, replicas=1, environment=None):
    response = {
        'job_id': None,
        'message': None,
        'status': global_vars.STATUS_OK,
    }
    try:
        namespace = get_namespace()
        if not job_id:
            job_id = generate_uuid()
        if not run_id:
            run_id = job_id
        job_name = get_job_name_from_id(job_id)
        # config_map_name = f'''{job_name}-positions'''
        job_root_dir = get_job_root_dir_from_id(job_id)
        job_output_dir = os.path.join(job_root_dir, 'out')

        ## Set environment-specific configuration for Job definition
        templateFile = "job.tpl.yaml"
        project_subpath = ''
        image_tag = config['uws']['job']['image']['tag']
        for envvar in environment:
            ## Allow individual jobs to override the image tag via the environment variables
            if envvar['name'] == 'UWS_JOB_IMAGE_TAG':
                image_tag = envvar['value']
        ## Set the image pull policy if specified; otherwise set semi-intelligently
        try:
            pullPolicy = config['uws']['job']['image']['pullPolicy']
        except:
            pullPolicy = 'Always' if image_tag in ['latest', 'dev'] else 'IfNotPresent'

        # all_volumes = [{
        #     'name': 'positions-volume',
        #     'configMapName': config_map_name,
        #     'mountPath': '/etc/cutout/positions.csv',
        #     'subPath': 'positions.csv',
        #     'readOnly': True,
        # }]
        all_volumes = []
        for volume in config['uws']['volumes']:
            all_volumes.append(volume)

        jobCompleteApiUrl = f'''{config['server']['protocol']}://{os.path.join(
            config['server']['hostName'],
            config['server']['basePath'],
            config['server']['apiBasePath'],
            'uws/report'
        )}'''

        with open(os.path.join(os.path.dirname(__file__), 'templates', templateFile)) as f:
            templateText = f.read()
        template = Template(templateText)

        job_body = yaml.safe_load(template.render(
            name=job_name,
            runId=run_id,
            jobId=job_id,
            ownerId=owner_id,
            namespace=namespace,
            backoffLimit=0,
            replicas=replicas,
            ## CAUTION: Discrepancy between the UID of the image user and the UWS API server UID
            ##          will create permissions problems. For example, if the job UID is 1001 and
            ##          the server UID is 1000, then files created by the job will not in general
            ##          allow the server to delete them when cleaning up deleted jobs.
            image={
                'repository': image_repo,
                'tag': image_tag,
                'pull_policy': pullPolicy,
            },
            imageJobMonitor={
                'repository': config['uws']['job']['imageJobMonitor']['repository'],
                'tag': config['uws']['job']['imageJobMonitor']['tag'],
                'pull_policy': config['uws']['job']['imageJobMonitor']['pullPolicy'],
            },

            # Main command being run on the Job container
            # This command does the following:
                # Creates the input fasta file
                # Runs the CLEAN command on the input file and stores the output to log file at /uws/jobs/<jobId>/out/log
                # In case the CLEAN command has an exception, an error file is created at  /uws/jobs/<jobId>/out/error for which the monitor looks for
                # In an error condition, we still want the entire command to be False so that the "finished" file is not created (it is created as {command} && touch finished), therefore, error condition is ANDed with False
            command=command,

            environment=environment,
            uws_root_dir=config['uws']['workingVolume']['mountPath'],
            job_output_dir=job_output_dir,
            project_subpath=project_subpath,
            securityContext=config['uws']['job']['securityContext'],
            workingVolume=config['uws']['workingVolume'],
            volumes=all_volumes,
            resources=config['uws']['job']['resources'],
            # apiToken=config['jwt']['hs256Secret'],
            apiToken='dummy',
            jobCompleteApiUrl=jobCompleteApiUrl,
            # releaseName=os.environ.get('RELEASE_NAME', 'dummy'),
            ttlSecondsAfterFinished=config['uws']['job']['ttlSecondsAfterFinished'],
            activeDeadlineSeconds=config['uws']['job']['activeDeadlineSeconds'],
            monitorEnabled=config['uws']['job']['monitorEnabled'],
        ))
        log.debug("Job {}:\n{}".format(job_name, yaml.dump(job_body, indent=2)))
        api_response = api_batch_v1.create_namespaced_job(
            namespace=namespace, body=job_body
        )
        response['job_id'] = job_id

        log.debug(f"Job {job_name} created")
    # TODO: Is there additional information to obtain from the ApiException?
    # except ApiException as e:
    #     msg = str(e)
    #     log.error(msg)
    #     response['status'] = global_vars.STATUS_ERROR
    #     response['message'] = msg
    except Exception as e:
        msg = str(e)
        log.error(msg)
        response['message'] = msg
        response['status'] = global_vars.STATUS_ERROR
    return response
