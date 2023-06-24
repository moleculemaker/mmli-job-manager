import json
import logging
import logging.config
import string
import random
import sys
import time
import threading

import urllib3
from kubernetes import watch, client, config as kubeconfig
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream
#from kubernetes.client import ApiException
from requests import HTTPError

from dbconnector import DbConnector
from global_vars import config, log
import kubejob

CRD_GROUP_NAME = "ndslabs.org"
CRD_VERSION_V1 = "v1"
CRD_APPSPECS_PLURAL = "workbenchappspecs"
CRD_USERAPPS_PLURAL = "workbenchuserapps"
CRD_APPSPECS_KIND = "WorkbenchAppSpec"
CRD_USERAPPS_KIND = "WorkbenchUserApp"

BASE_URL = 'http://localhost:5000'

custom = client.CustomObjectsApi()



# TODO: V2 - watch custom resources and react accordingly
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

def determine_new_status(type, phase, conditions):
    service_status = phase  # default is no phase change

    # TODO: Update stack service phase according to phase/type
    if phase == 'Pending':  # TODO: initial scheduling phase
        service_status = 'initializing'
    elif phase == 'Error':  # TODO: errors at startup/runtime
        service_status = 'error'
    elif phase == 'Running' and type != 'DELETED':  # TODO: errors at startup/runtime
        service_status = 'started'
    elif type == 'ADDED':  # TODO: 'starting' incremental phase updates
        service_status = 'created'
    elif type == 'DELETED':  # TODO: 'stopping' incremental phase updates
        service_status = 'stopped'
    elif type == 'MODIFIED':  # TODO: granular incremental phase updates (conditions?)
        service_status = 'starting'

    return service_status


def write_status(job_id, job_status, username):
    username = get_username(username)
    job = data_store.retrieve_userapp_by_id(userapp_id=userapp_id, username=username)
    if job is not None:
        # Only write if needed (ignore no-ops)
        should_update = False



        # Assume success?
        if should_update:
            result = data_store.update_userapp(userapp)
    else:
        log.warning('Unable to update status and endpoints: Userapp not found: %s %s' % (username, userapp_id))


class KubeEventWatcher:

    def __init__(self):
        self.logger = logging.getLogger('kube-event-watcher')
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

        # Include workbench app labels
        # Example:      'labels': {'manager': 'workbench',
        #                          'pod-template-hash': '977967b76',
        #                          'user': 'test',
        #                          'workbench-app': 's00402'}
        # TODO: Parameterize this?
        required_labels = {
            'type': 'uws-job'
        }
        self.logger.info('KubeWatcher looking for required labels: ' + str(required_labels))

        timeout_seconds = 0
        resource_version = ''
        k8s_event_stream = None

        w = watch.Watch()

        while True:
            time.sleep(1)
            self.logger.info('KubeWatcher is connecting...')
            try:
                # Resource version is used to keep track of stream progress (in case of resume)
                k8s_event_stream = w.stream(func=kubejob.api_batch_v1.list_namespaced_job,
                                            namespace=kubejob.get_namespace(),
                                            timeout_seconds=timeout_seconds,
                                            resource_version=resource_version)

                self.logger.info('KubeWatcher connected!')

                # Parse events in the stream for Pod phase updates
                for event in k8s_event_stream:
                    resource_version = event['object'].metadata.resource_version

                    # Skip Pods in ignored namespaces
                    if event['object'].metadata.namespace in ignored_namespaces:
                        self.logger.debug('Skipping event in excluded namespace')
                        continue

                    # Examine labels, ignore if not workbench app
                    # self.logger.debug('Event recv\'d: %s' % event)
                    labels = event['object'].metadata.labels

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
                        self.log.warning('WARNING: Invalid number of segments -  JobName=%s' % name)
                        continue

                    # uws-job-jobid => we want last segment
                    job_id = segments[-1]

                    type = event['type']
                    status = event['object'].status
                    conditions = status.conditions

                    self.logger.debug(f'Event: job_id={job_id}   type={type}   status={status}')
                    new_phase = None
                    if type == 'MODIFIED' and conditions is None:
                        new_phase = 'executing'
                    elif type == 'MODIFIED' and len(conditions) > 0 and conditions[0].type == 'Complete':
                        new_phase = 'completed'
                    elif type == 'MODIFIED' and status.failed > 0:
                        new_phase = 'error'
                    else:
                        self.logger.debug(f'>> Skipped job update: {job_id}-> {new_phase}')
                        self.logger.debug(f'>> Status: {str(status)}')

                    if new_phase is not None:
                        self.logger.debug('Updating job phase: %s -> %s' % (job_id, new_phase))
                        self.db.update_job(
                            job_id=job_id,
                            phase=new_phase,
                        )
                        self.logger.debug('Updated job phase: %s -> %s' % (job_id, new_phase))


                    # Calculate new status/endpoints and write to db
                    #job_status = determine_new_status(type, phase, conditions)
                    #write_status(job_id, job_status)

                    #log.info(
                    #    'UserappId=%s  ServiceKey=%s  type=%s  phase=%s  ->  status=%s  endpoints=%s' % (
                    #    userapp_id, service_key, type, phase, service_status, str(service_endpoints)))
            except urllib3.exceptions.ProtocolError as e:
                self.logger.error('KubeWatcher reconnecting to Kube API: %s' % str(e))
                if k8s_event_stream:
                    k8s_event_stream.close()
                k8s_event_stream = None
                time.sleep(2)
                continue
            except (ApiException, HTTPError) as e:
                if k8s_event_stream:
                    k8s_event_stream.close()
                k8s_event_stream = None
                self.logger.error("Connection to kube API failed: " + str(e))
                if e.status == 410:
                    # Resource too old
                    resource_version = None
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


def open_exec_userapp_interactive(user, ssid, ws):
    v1 = client.CoreV1Api()

    namespace = get_resource_namespace(username=user)
    pod_name = get_pod_name(user=user, ssid=ssid)

    # Calling exec interactively
    # TODO: parameterize via spec field
    exec_command = ['/bin/sh', '-c', '(bash || ash || sh)']
    resp = stream(v1.connect_get_namespaced_pod_exec,
                  pod_name,
                  namespace,
                  command=exec_command,
                  stderr=True, stdin=True,
                  stdout=True, tty=True,
                  _preload_content=False)

    try:
        while resp.is_open():
            # Grab command string data from Websocket (without blocking)
            user_input = ws.receive(timeout=0)

            # otherwise send to stdin
            if user_input:
                log.debug('Sending command: ' + user_input)
                resp.write_stdin(user_input)

            if resp.is_open():
                # read command stdout/stderr without blocking
                resp.update(timeout=0)
                if resp.peek_stdout(timeout=0):
                    ws.send(resp.read_stdout(timeout=0))
                if resp.peek_stderr(timeout=0):
                    ws.send(resp.read_stderr(timeout=0))
    except Exception as e:
        log.exception(" >>> Exception encountered:", e)
    finally:
        ws.send('CONNECTION CLOSED')
        # ws.close()
        if resp.is_open():
            resp.close()


def generate_random_password(length=16):
    # choose from all lowercase letter
    letters = string.ascii_letters + string.digits + string.punctuation
    result_str = ''.join(random.choice(letters) for i in range(length))
    log.debug("Random string of length=%d is: %s" % (length, result_str))
    return result_str


# Workbench-specific helpers
def get_stack_service_id(*args):
    return "-".join(args)


def get_resource_name(*args):
    if KUBE_WORKBENCH_RESOURCE_PREFIX:
        return "%s-%s" % (KUBE_WORKBENCH_RESOURCE_PREFIX, "-".join(args))
    else:
        return "-".join(args)


def get_username(username):
    return username.replace('@', '').replace('.', '')


def get_resource_namespace(username):
    if is_single_namespace():
        return KUBE_WORKBENCH_NAMESPACE
    else:
        # TODO: Prefix with KUBE_WORKBENCH_RESOURCE_PREFIX?
        # TODO: better to use KUBE_WORKBENCH_NAMESPACE?
        return get_username(username)


# TODO: Replace with explicit boolean
def is_single_pod():
    return config.KUBE_WORKBENCH_SINGLEPOD


# TODO: Replace with explicit boolean
def is_single_namespace():
    return True if config.KUBE_WORKBENCH_NAMESPACE is not None and config.KUBE_WORKBENCH_NAMESPACE != '' else False


#
#
#
# Simplified API for Workbench endpoints to access
#
#
#
def initialize():
    try:
        kubeconfig.load_incluster_config()
    except:
        log.warning('Failed to load in-cluster config, trying kubeconfig file')
        try:
            kubeconfig.load_kube_config()
        except:
            log.warning('Failed to load any cluster config.. this might not work.')

    host = kubeconfig.kube_config.Configuration().host
    logging.info("Connecting to Kubernetes API: {}".format(host))



# Create necessary resources for a new user
def init_user(username):
    username = get_username(username)
    namespace = get_resource_namespace(username)

    # Ensure user home PVC exists
    pvc_name = get_home_pvc_name(username)
    storage_class = get_home_storage_class()
    if not is_single_namespace():
        try:
            create_namespace(namespace_name=namespace)
        except ApiException as e:
            # Ignore conflict - creation of these resources is idempotent
            if e.status != 409:
                raise e

    try:
        create_persistent_volume_claim(namespace=namespace, pvc_name=pvc_name, storage_class=storage_class)
    except ApiException as e:
        # Ignore conflict - creation of these resources is idempotent
        if e.status != 409:
            raise e

    # create_resource_quota(namespace=namespace, quota_name=username, hard_quotas=DEFAULT_DEV_RESOURCE_QUOTA)
    # create_network_policy(namespace=username, policy_name=username)
    # TODO: create_service_account()


def get_init_container(username, spec_key, svc_key):
    return {'name': 'wait-for', 'image': 'ghcr.io/groundnuty/k8s-wait-for:v1.6', 'imagePullPolicy': 'Always',
            'args': ['pod', '-lworkbench_svc']}


# Creates the Kubernetes resources related to a userapp
def create_userapp(username, userapp, spec_map):
    namespace = get_resource_namespace(username)
    containers = []
    ingress_hosts = {}
    userapp_id = userapp['id']
    userapp_key = userapp['key']
    should_run_as_single_pod = userapp['singlePod'] if 'singlePod' in userapp else config.KUBE_WORKBENCH_SINGLEPOD

    labels = {
        'manager': 'workbench',
        'user': get_username(username),
        'workbench-app': userapp_id
    }

    #log.debug("Map of specs: %s" % spec_map)
    for stack_service in userapp['services']:
        service_key = stack_service['service']
        app_spec = spec_map.get(service_key, None)
        svc_labels = labels.copy()
        svc_labels['workbench-svc'] = service_key
        if app_spec is None:
            log.error("Failed to find app_spec: %s" % service_key)
            raise AppSpecNotFoundError("Failed to find app_spec: %s" % service_key)
        stack_service_id = get_stack_service_id(userapp_id, service_key)
        resource_name = get_resource_name(get_username(username), userapp_id, service_key)
        service_ports = app_spec['ports'] if 'ports' in app_spec else []
        ingress_hosts[resource_name] = service_ports

        # Build up config from userapp env/config and appspec config
        configmap_data = stack_service['config'] if 'config' in stack_service else {}
        spec_config = app_spec['config'] if 'config' in app_spec else []

        # TODO: Support to / from other service envs
        for cfg in spec_config:
            if 'canOverride' in cfg and not cfg['canOverride']:
                # reset to spec value if we can't override
                configmap_data[cfg['name']] = cfg['value'] if 'value' in cfg else ''
            if ('isPassword' in cfg and cfg['isPassword']) and \
                    ('canOverride' in cfg and cfg['canOverride']) and \
                    ('value' not in cfg or not cfg['value']):
                # generate password if none is provided
                configmap_data[cfg['name']] = generate_random_password()
            else:
                configmap_data[cfg['name']] = cfg['value'] if 'value' in cfg else ''

        stack_service['config'] = configmap_data

        # TODO: Support custom volume mounts?
        userapp_mounts = {}
        volume_mounts = []
        if 'volumeMounts' in app_spec:
            for vol in app_spec['volumeMounts']:
                # If path is the root
                if 'defaultPath' in vol:
                    if vol['defaultPath'] != '/':
                        # default path is not the root, use it as a relative path
                        if str.startswith(vol['defaultPath'], '/'):
                            # Make path relative if given an absolute
                            sub_path = vol['defaultPath'][1:]
                        else:
                            # Path is relative, use as-is
                            sub_path = vol['defaultPath']
                    else:
                        # default path is the root - mount the whole volume (no sub_path)
                        sub_path = ''
                else:
                    # No default path specified, make a local folder
                    sub_path = f'AppData/{userapp_id}-{service_key}'
                mount_path = vol['mountPath']
                userapp_mounts[sub_path] = mount_path
                volume_mounts.append(client.V1VolumeMount(
                    name="home",
                    mount_path=mount_path,
                    sub_path=sub_path,
                    read_only=vol['readOnly'] if 'readOnly' in vol else False
                ))

        stack_service['volume_mounts'] = userapp_mounts

        # Create one container per-stack service
        container = {
            'name': stack_service_id,
            'resourceLimits': stack_service['resourceLimits'] if 'resourceLimits' in stack_service else app_spec[
                'resourceLimits'] if 'resourceLimits' in app_spec else {},
            'command': stack_service['command'] if 'command' in stack_service else app_spec[
                'command'] if 'command' in app_spec else None,
            'args': stack_service['args'] if 'args' in stack_service else app_spec[
                'args'] if 'args' in app_spec else None,
            'image': stack_service['image'] if 'image' in stack_service else app_spec['image'],
            'configmap': resource_name,
            'security_context': app_spec['securityContext'] if 'securityContext' in app_spec else None,
            'volume_mounts': volume_mounts,
            'prestop': stack_service['prestop'] if 'prestop' in stack_service else None,
            'poststart': stack_service['poststart'] if 'poststart' in stack_service else None,
            'ports': service_ports,
        }

        containers.append(container)

        # Create one Kubernetes service per-stack service
        if len(service_ports) > 0:
            create_service(service_name=resource_name,
                           namespace=namespace, labels=svc_labels,
                           service_ports=service_ports)

        # Create one Kubernetes configmap per-stack service
        create_configmap(namespace=namespace, configmap_name=resource_name, configmap_data=configmap_data)

        init_containers = []
        if not should_run_as_single_pod:
            #         - name: wait-for-volume-ceph
            #           image:
            #           imagePullPolicy: Always
            #           args:
            #             - "pod"
            #             - "-lapp=develop-volume-ceph-krakow"
            if 'depends' in app_spec:
                init_containers = [
                    client.V1Container(name='wait-for-dep-' + dep['key'],
                                       image='ghcr.io/groundnuty/k8s-wait-for:v1.6',
                                       image_pull_policy='Always',
                                       args=[
                                           "pod",
                                           "-lmanager=workbench",
                                           "-lworkbench-app=" + userapp_id,
                                           "-luser=" + get_username(username),
                                           "-lworkbench-svc=" + dep['key']
                                       ]) for dep in app_spec['depends'] if dep['required']
                ]

            service_account = backend_config['userapps']['service_account_name'] if 'userapps' in backend_config and 'service_account_name' in backend_config['userapps'] else None

            # Create one deployment per-stack (start with 0 replicas, aka "Stopped")
            create_deployment(deployment_name=resource_name,
                              namespace=namespace,
                              replicas=0,
                              service_account=service_account,
                              username=get_username(username),
                              init_containers=init_containers,
                              labels=svc_labels,
                              containers=[container],
                              collocate=userapp_id if 'collocate' in app_spec and app_spec['collocate'] else False)

    # Create one ingress per-stack
    if len(ingress_hosts.keys()) > 0:
        userapp_annotations = backend_config['userapps']['ingress']['annotations'] \
            if 'userapps' in backend_config \
               and 'ingress' in backend_config['userapps'] \
               and 'annotations' in backend_config['userapps']['ingress'] else {}
        ingress_class_name = backend_config['userapps']['ingress']['class'] \
            if 'userapps' in backend_config \
               and 'ingress' in backend_config['userapps'] \
               and 'class' in backend_config['userapps']['ingress'] else None
        ingress_tls_secret_name = backend_config['userapps']['ingress']['tls']['secretName'] \
            if 'userapps' in backend_config \
               and 'ingress' in backend_config['userapps'] \
               and 'tls' in backend_config['userapps']['ingress'] \
               and 'secretName' in backend_config['userapps']['ingress']['tls'] else None
        create_ingress(ingress_name=get_resource_name(get_username(username), userapp_id, userapp_key),
                       namespace=namespace, labels=labels,
                       ingress_hosts=ingress_hosts,
                       ingress_tls_secret_name=ingress_tls_secret_name,
                       annotations=userapp_annotations,
                       ingress_class_name=ingress_class_name)

    if should_run_as_single_pod:
        service_account = backend_config['userapps']['service_account_name'] if 'userapps' in backend_config and 'service_account_name' in backend_config['userapps'] else None

        # No need to collocate, since all will run in single pod
        # Create one deployment per-stack (start with 0 replicas, aka "Stopped")
        create_deployment(deployment_name=get_resource_name(get_username(username), userapp_id, userapp_key),
                          namespace=namespace,
                          replicas=0,
                          service_account=service_account,
                          username=get_username(username),
                          labels=labels,
                          # TODO: how to wait for deps in singlepod mode?
                          # init_containers=init_containers,
                          containers=containers)


def update_userapp_replicas(username, userapp_id, replicas):
    userapp = data_store.retrieve_userapp_by_id(userapp_id=userapp_id, username=username)
    if userapp is None:
        return False
    spec_key = userapp['key']

    name = get_resource_name(get_username(username), userapp_id, spec_key)
    namespace = get_resource_namespace(username)
    result = patch_scale_deployment(deployment_name=name, namespace=namespace, replicas=replicas)

    if result is None:
        return False

    return True


def update_userapp(username, userapp_id, userapp):
    for svc in userapp['services']:
        service_key = svc['service']
        resource_name = get_resource_name(get_username(username), userapp_id, service_key)
        namespace = get_resource_namespace(username)

        # Build up config from userapp env/config and appspec config
        configmap_data = svc['config'] if 'config' in svc else {}

        log.debug("Saving configmap data: " + str(configmap_data))

        update_configmap(namespace=namespace, configmap_name=resource_name, configmap_data=configmap_data)


def patch_scale_userapp(username, userapp, replicas):
    userapp_id = userapp['id']
    namespace = get_resource_namespace(username)

    should_run_as_single_pod = userapp['singlePod'] if 'singlePod' in userapp else config.KUBE_WORKBENCH_SINGLEPOD
    if should_run_as_single_pod:
        deployment_name = get_resource_name(get_username(username), userapp_id, userapp['key'])
        patch_scale_deployment(deployment_name=deployment_name, namespace=namespace, replicas=replicas)
        return True
    else:
        results = []
        # TODO: how to check results
        for stack_service in userapp['services']:
            service_key = stack_service['service']
            deployment_name = get_resource_name(get_username(username), userapp_id, service_key)
            results.append(
                patch_scale_deployment(deployment_name=deployment_name, namespace=namespace, replicas=replicas))
        return False not in results


# Cleans up the Kubernetes resources related to a userapp
def destroy_userapp(username, userapp):
    userapp_id = userapp['id']
    userapp_key = userapp['key']
    name = get_resource_name(get_username(username), userapp_id, userapp_key)
    namespace = get_resource_namespace(username)

    log.debug(f'Deleting Ingress: {name}')
    delete_ingress(name=name, namespace=namespace)
    # TODO: networkpolicy? (currently unused)

    should_run_as_single_pod = userapp['singlePod'] if 'singlePod' in userapp else config.KUBE_WORKBENCH_SINGLEPOD
    if should_run_as_single_pod:
        log.debug(f'Deleting Deployment (singlepod): {name}')
        delete_deployment(name=name, namespace=namespace)

    for stack_service in userapp['services']:
        service_key = stack_service['service']
        name = get_resource_name(get_username(username), userapp_id, service_key)
        if not should_run_as_single_pod:
            log.debug(f'Deleting Deployment ({service_key}): {name}')
            delete_deployment(name=name, namespace=namespace)
        log.debug(f'Deleting Service: {name}')
        delete_service(name=name, namespace=namespace)
        log.debug(f'Deleting ConfigMap: {name}')
        delete_configmap(name=name, namespace=namespace)

    return


#
#
# K8S entity helpers
#
#
def create_namespace(namespace_name, **kwargs):
    v1 = client.CoreV1Api()

    # TODO: Validation
    namespace_labels = kwargs['labels'] if 'labels' in kwargs else {}
    namespace_annotations = kwargs['annotations'] if 'annotations' in kwargs else {}

    namespace = v1.create_namespace(body=client.V1Namespace(
        api_version='v1',
        kind='Namespace',
        metadata=client.V1ObjectMeta(
            name=namespace_name,
            annotations=namespace_annotations,
            labels=namespace_labels
        ),
    ))
    log.debug("Created namespace resource: " + str(namespace))
    return namespace


def delete_namespace(name):
    v1 = client.CoreV1Api()
    v1.delete_namespace(name=name)
    log.debug("Deleted namespace resource: %s" % name)
    return


def get_deployment(name, namespace):
    try:
        return client.AppsV1Api().read_namespaced_deployment(name=name, namespace=namespace)
    except (ApiException, HTTPError) as exc:
        if isinstance(exc, ApiException) and exc.status == 404:
            return None
        else:
            log.error("Error reading deployment resource: %s" % str(exc))
            raise exc


def patch_scale_deployment(deployment_name, namespace, replicas) -> bool:
    # No-op if we can't find the deployment
    deployment = get_deployment(name=deployment_name, namespace=namespace)
    log.info(f'Patching {deployment_name} to replicas={str(replicas)}')
    if deployment is None:
        # TODO: Raise an error here?
        log.error("Failed to find deployment: " + str(deployment_name))
        return False

    # No-op if we already have our desired number of replicas
    current_repl = deployment.spec.replicas
    if current_repl == replicas:
        log.debug("No-op for setting replicas number: %d -> %d" % (current_repl, replicas))
        return False

    # Query number of replicas
    result = client.AppsV1Api().patch_namespaced_deployment_scale(namespace=namespace, name=deployment_name,
                                                                  body={'spec': {'replicas': replicas}})
    log.debug("Patch Result: " + str(result))
    return result


def create_configmap(configmap_name, configmap_data, **kwargs):
    v1 = client.CoreV1Api()

    # TODO: Validation
    configmap_namespace = kwargs['namespace'] if 'namespace' in kwargs else 'default'
    configmap_labels = kwargs['labels'] if 'labels' in kwargs else {}
    configmap_annotations = kwargs['annotations'] if 'annotations' in kwargs else {}

    try:
        configmap = v1.create_namespaced_config_map(namespace=configmap_namespace, body=client.V1ConfigMap(
            api_version='v1',
            kind='ConfigMap',
            metadata=client.V1ObjectMeta(
                name=configmap_name,
                namespace=configmap_namespace,
                annotations=configmap_annotations,
                labels=configmap_labels
            ),
            data=configmap_data
        ))
        log.debug("Created configmap resource: " + str(configmap))
        return configmap

    except (ApiException, HTTPError) as exc:
        if isinstance(exc, ApiException) and exc.status == 409:
            return None
        else:
            log.error("Error creating configmap resource: %s" % str(exc))
            raise exc


def update_configmap(configmap_name, configmap_data, **kwargs):
    v1 = client.CoreV1Api()
    configmap_namespace = kwargs['namespace'] if 'namespace' in kwargs else 'default'
    configmap_labels = kwargs['labels'] if 'labels' in kwargs else {}
    configmap_annotations = kwargs['annotations'] if 'annotations' in kwargs else {}

    return v1.replace_namespaced_config_map(name=configmap_name,
                                            namespace=configmap_namespace,
                                            body=client.V1ConfigMap(
                                                api_version='v1',
                                                kind='ConfigMap',
                                                metadata=client.V1ObjectMeta(
                                                    name=configmap_name,
                                                    namespace=configmap_namespace,
                                                    annotations=configmap_annotations,
                                                    labels=configmap_labels
                                                ),
                                                data=configmap_data
                                            ))


def retrieve_configmap(configmap_name, **kwargs):
    v1 = client.CoreV1Api()
    configmap_namespace = kwargs['namespace'] if 'namespace' in kwargs else 'default'
    configmap = v1.read_namespaced_config_map(configmap_name, configmap_namespace)

    return configmap.data


def delete_configmap(name, namespace):
    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_config_map(name=name, namespace=namespace)
        log.debug("Deleted configmap resource: %s/%s" % (namespace, name))
    except (ApiException, HTTPError) as exc:
        if not isinstance(exc, ApiException) or exc.status != 404:
            log.error("Error deleting configmap resource: %s" % str(exc))
            raise exc


def create_persistent_volume_claim(pvc_name, **kwargs):
    v1 = client.CoreV1Api()

    # TODO: Validation
    pvc_namespace = kwargs['namespace'] if 'namespace' in kwargs else 'default'
    pvc_labels = kwargs['labels'] if 'labels' in kwargs else {}
    pvc_annotations = kwargs['annotations'] if 'annotations' in kwargs else {}

    pvc_storage_class = kwargs['storage_class'] if 'storage_class' in kwargs else ''
    pvc_volume_mode = kwargs['volume_mode'] if 'volume_mode' in kwargs else 'Filesystem'
    pvc_access_modes = kwargs['access_modes'] if 'access_modes' in kwargs else ['ReadWriteMany']
    pvc_request_storage = kwargs['request_storage'] if 'request_storage' in kwargs else '10Mi'
    pvc_volume_name = kwargs['volume_name'] if 'volume_name' in kwargs else None

    if pvc_volume_name:
        log.warning("Warning: PVC Volume Name is set - manual claim binding may be necessary.")

    pvc_spec = client.V1PersistentVolumeClaimSpec(
        volume_name=pvc_volume_name,
        access_modes=pvc_access_modes,
        volume_mode=pvc_volume_mode,
        resources=client.V1ResourceRequirements(
            requests={
                'storage': pvc_request_storage
            }
        )
    )

    if pvc_storage_class != '':
        pvc_spec.storage_class_name = pvc_storage_class

    pvc = v1.create_namespaced_persistent_volume_claim(namespace=pvc_namespace, body=client.V1PersistentVolumeClaim(
        api_version='v1',
        kind='PersistentVolumeClaim',
        metadata=client.V1ObjectMeta(
            name=pvc_name,
            namespace=pvc_namespace,
            annotations=pvc_annotations,
            labels=pvc_labels
        ),
        spec=pvc_spec
    ))
    log.debug("Created pvc resource: " + str(pvc))
    return pvc


def get_image_string(container_image):
    # Required values
    name = container_image.get('name', '')

    # Optional values
    repo = container_image.get('repo', None)
    tags = container_image.get('tags', [])
    tag = tags[0] if len(tags) > 0 else 'latest'
    return '%s/%s:%s' % (repo, name, tag) if repo is not None else '%s:%s' % (name, tag)


def get_home_pvc_name(user):
    pvc_suffix = backend_config['userapps']['home_storage'][
        'claim_suffix'] if 'userapps' in backend_config and 'home_storage' in backend_config[
        'userapps'] and 'claim_suffix' in backend_config['userapps']['home_storage'] else 'home-data'
    return get_resource_name(get_username(user), pvc_suffix)


def get_home_storage_class():
    return backend_config['userapps']['home_storage'][
        'storage_class'] if 'userapps' in backend_config and 'home_storage' in backend_config[
        'userapps'] and 'storage_class' in backend_config['userapps']['home_storage'] else None


# Containers:
#   "busybox" -> { name, configmap, image, lifecycle, ports, command }
def create_deployment(deployment_name, containers, labels, username, **kwargs):
    appv1 = client.AppsV1Api()

    # TODO: Validation
    deployment_labels = labels
    deployment_namespace = kwargs['namespace'] if 'namespace' in kwargs else 'default'
    deployment_annotations = kwargs['annotations'] if 'annotations' in kwargs else {}
    deployment_collocate_label = kwargs['collocate'] if 'collocate' in kwargs else False
    deployment_replicas = kwargs['replicas'] if 'replicas' in kwargs else 0

    init_containers = kwargs['init_containers'] if 'init_containers' in kwargs else []

    podspec_annotations = kwargs['pod_annotations'] if 'pod_annotations' in kwargs else {}

    service_account_name = kwargs['service_account'] if 'service_account' in kwargs else None

    # Mount in user home / shared storage, if necessary
    shared_volumes = []
    shared_volume_mounts = []
    enable_home_storage = backend_config['userapps']['home_storage'][
        'enabled'] if 'userapps' in backend_config and 'home_storage' in backend_config[
        'userapps'] and 'enabled' in backend_config['userapps']['home_storage'] else False
    if enable_home_storage:
        home_pvc_name = get_home_pvc_name(username)
        shared_volume_mounts += client.V1VolumeMount(name="home", mount_path='/home/' + username, read_only=False),
        shared_volumes += client.V1Volume(name="home", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
            claim_name=home_pvc_name,
            read_only=False
        )),

    enable_shared_storage = backend_config['userapps']['shared_storage'][
        'enabled'] if 'userapps' in backend_config and 'shared_storage' in backend_config[
        'userapps'] and 'enabled' in backend_config['userapps']['shared_storage'] else False
    if enable_shared_storage:
        shared_storage_claim_name = backend_config['userapps']['shared_storage'][
            'claim_name'] if 'userapps' in backend_config and 'shared_storage' in backend_config[
            'userapps'] and 'claim_name' in backend_config['userapps']['shared_storage'] else 'workbench-shared-storage'
        shared_storage_mount_path = backend_config['userapps']['shared_storage'][
            'mount_path'] if 'userapps' in backend_config and 'shared_storage' in backend_config[
            'userapps'] and 'mount_path' in backend_config['userapps']['shared_storage'] else '/shared'
        shared_storage_read_only = backend_config['userapps']['shared_storage'][
            'read_only'] if 'userapps' in backend_config and 'shared_storage' in backend_config[
            'userapps'] and 'read_only' in backend_config['userapps']['shared_storage'] else False
        shared_volume_mounts += client.V1VolumeMount(name="shared", mount_path=shared_storage_mount_path, read_only=shared_storage_read_only),
        shared_volumes += client.V1Volume(name="shared", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
            claim_name=shared_storage_claim_name,
            read_only=shared_storage_read_only
        )),

    # Build a podspec from given containers and other parameters
    podspec = client.V1PodSpec(
        service_account_name=service_account_name,
        volumes=shared_volumes,
        init_containers=init_containers,
        containers=[
            client.V1Container(
                name=container['name'],
                command=container['command'],
                args=container['args'],
                security_context=client.V1SecurityContext(
                    capabilities=client.V1Capabilities(
                        add=container['security_context']['capabilities']['add'] if 'add' in container['security_context']['capabilities'] else None
                    ) if 'capabilities' in container['security_context'] and container['security_context']['capabilities'] else None,
                    privileged=container['security_context']['privileged'] if 'security_context' in container and 'privileged' in container['security_context'] else None,
                    run_as_user=container['security_context']['runAsUser'] if 'runAsUser' in container['security_context'] else None,
                    run_as_group=container['security_context']['runAsGroup'] if 'runAsGroup' in container['security_context'] else None,
                    run_as_non_root=container['security_context']['runAsNonRoot'] if 'runAsNonRoot' in container['security_context'] else None,
                    proc_mount=container['security_context']['procMount'] if 'procMount' in container['security_context'] else None,
                    read_only_root_filesystem=container['security_context']['readOnlyRootFilesystem'] if 'readOnlyRootFilesystem' in container['security_context'] else None,
                    allow_privilege_escalation=container['security_context']['allowPrivilegeEscalation'] if 'allowPrivilegeEscalation' in container['security_context'] else None,

                ) if 'security_context' in container and container['security_context'] else None,
                volume_mounts=shared_volume_mounts + (container['volume_mounts'] if 'volume_mounts' in container else []),
                # TODO: support resource limits?
                # resources=V1ResourceRequirements(),


                # create configmap with env vars
                env_from=[
                    client.V1EnvFromSource(
                        config_map_ref=client.V1ConfigMapEnvSource(
                            name=container['configmap']
                        )
                    )
                ],
                #
                # TODO: support container.lifecycle?
                lifecycle=container['lifecycle'] if 'lifecycle' in container else None,
                image=get_image_string(container['image']),
                ports=[
                    client.V1ContainerPort(
                        name='%s%s' % (port['protocol'] if 'protocol' in port else 'tcp', str(port['port'])),
                        container_port=port['port'],
                        protocol='TCP'
                        # port['protocol'].upper() if 'protocol' in port and port['protocol'] != 'http' else
                    ) for port in container['ports']
                ]
            ) for container in containers
        ])

    # Schedule pods on same node, if requested
    if deployment_collocate_label:
        podspec.affinity = client.V1Affinity(
            pod_affinity=client.V1PodAffinity(
                required_during_scheduling_ignored_during_execution=[
                    client.V1PodAffinityTerm(
                        topology_key="kubernetes.io/hostname",
                        label_selector=client.V1LabelSelector(
                            match_expressions=[
                                client.V1LabelSelectorRequirement(
                                    key="workbench-app",
                                    operator="In",
                                    values=[deployment_collocate_label]
                                )
                            ]
                        )
                    )
                ]
            )
        )

    try:
        body = client.V1Deployment(
            api_version='apps/v1',
            kind='Deployment',
            metadata=client.V1ObjectMeta(
                name=deployment_name,
                namespace=deployment_namespace,
                labels=deployment_labels,
                annotations=deployment_annotations
            ),
            spec=client.V1DeploymentSpec(
                replicas=deployment_replicas,
                selector={"matchLabels": deployment_labels},
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        name=deployment_name,
                        namespace=deployment_namespace,
                        annotations=podspec_annotations,
                        labels=deployment_labels
                    ),
                    spec=podspec,
                )
            )
        )

        log.info("Creating deployment resource: %s" % str(deployment_name))
        deployment = appv1.create_namespaced_deployment(namespace=deployment_namespace, body=body)
        log.debug("Created deployment resource: %s" % str(deployment))
        return deployment
    except (ApiException, HTTPError) as exc:
        if isinstance(exc, ApiException) and exc.status == 409:
            return None
        else:
            log.error("Error creating service resource: %s" % str(exc))
            raise exc


def delete_deployment(name, namespace):
    appv1 = client.AppsV1Api()
    try:
        appv1.delete_namespaced_deployment(name=name, namespace=namespace)
        log.debug("Deleted deployment resource: %s/%s" % (namespace, name))
    except (ApiException, HTTPError) as exc:
        if not isinstance(exc, ApiException) or exc.status != 404:
            log.error("Error deleting deployment resource: %s" % str(exc))
            raise exc


# Expected format:
#    service_ports = {
#        'http': 80,
#        'https': 443
#    }
def create_service(service_name, service_ports, labels, **kwargs):
    v1 = client.CoreV1Api()

    # TODO: Validation
    service_labels = labels
    service_namespace = kwargs['namespace'] if 'namespace' in kwargs else 'default'
    service_annotations = kwargs['annotations'] if 'annotations' in kwargs else {}

    try:
        log.info("Creating service with resource name: " + str(service_name))
        service = v1.create_namespaced_service(namespace=service_namespace, body=client.V1Service(
            api_version='v1',
            kind='Service',
            metadata=client.V1ObjectMeta(
                name=service_name,
                namespace=service_namespace,
                annotations=service_annotations,
                labels=service_labels
            ),
            spec=client.V1ServiceSpec(
                selector=service_labels,
                ports=[
                    client.V1ServicePort(
                        name=port['name'] if 'name' in port else None,
                        port=int(port['port']),
                        protocol='TCP'
                        # port['protocol'].upper() if 'protocol' in port and port['protocol'] != 'http' else
                    ) for port in service_ports
                ]
            )
        ))
        log.debug("Created service resource: " + str(service))
        return service

    except (ApiException, HTTPError) as exc:
        if isinstance(exc, ApiException) and exc.status == 409:
            return None
        else:
            log.error("Error creating service resource: %s" % str(exc))
            raise exc


def delete_service(name, namespace):
    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_service(name=name, namespace=namespace)

        log.debug("Deleted service resource: %s/%s" % (namespace, name))
    except (ApiException, HTTPError) as exc:
        if not isinstance(exc, ApiException) or exc.status != 404:
            log.error("Error deleting service resource: %s" % str(exc))
            raise exc


def create_ingress(ingress_name, ingress_hosts, labels, **kwargs):
    print("Creating ingress resource:")

    # TODO: Validation
    ingress_labels = labels
    ingress_namespace = kwargs['namespace'] if 'namespace' in kwargs else 'default'
    ingress_class_name = kwargs['ingress_class_name'] if 'ingress_class_name' in kwargs else None
    ingress_tls_secret_name = kwargs['ingress_tls_secret_name'] if 'ingress_tls_secret_name' in kwargs else None
    ingress_annotations = kwargs['annotations'] if 'annotations' in kwargs else {}

    ingress_domain = config.DOMAIN

    non_empty_rules = {}
    for service_name in ingress_hosts.keys():
        for port in ingress_hosts[service_name]:
            if service_name not in non_empty_rules:
                non_empty_rules[service_name] = []
            non_empty_rules[service_name] += port

    if len(non_empty_rules.keys()) > 0:
        try:
            ingress_input = client.V1Ingress(
                api_version='networking.k8s.io/v1',
                kind='Ingress',
                metadata=client.V1ObjectMeta(
                    name=ingress_name,
                    namespace=ingress_namespace,
                    annotations=ingress_annotations,
                    labels=ingress_labels,
                ),
                spec=client.V1IngressSpec(rules=[
                    client.V1IngressRule(
                        host='%s.%s' % (service_name, ingress_domain),
                        http=client.V1HTTPIngressRuleValue(
                            paths=[client.V1HTTPIngressPath(
                                path_type='ImplementationSpecific',
                                path='/',  # Since we use host-based routing
                                backend=client.V1IngressBackend(
                                    service=client.V1IngressServiceBackend(
                                        name=port['name'] if 'name' in port else service_name,
                                        port=client.V1ServiceBackendPort(
                                            number=port['port']
                                        )
                                    )
                                )
                            ) for port in ingress_hosts[service_name]]
                        )
                    ) for service_name in non_empty_rules.keys()
                ],
                    tls=[client.V1IngressTLS(hosts=[ingress_domain, '*.' + ingress_domain], secret_name=ingress_tls_secret_name)],
                    ingress_class_name=ingress_class_name)
            )

            ingress = client.NetworkingV1Api().create_namespaced_ingress_with_http_info(ingress_namespace, ingress_input)

            log.debug("Created ingress resource: " + str(ingress))
            # for i in ret.items:
            #    log.debug("%s\t%s\t%s" % (i.status.pod_ip, i.metadata.namespace, i.metadata.name))
            return ingress

        except (ApiException, HTTPError) as exc:
            if isinstance(exc, ApiException) and exc.status == 409:
                return None
            else:
                log.error("Error creating ingress resource: %s" % str(exc))
                raise exc


def delete_ingress(name, namespace):
    networkingv1 = client.NetworkingV1Api()
    try:
        networkingv1.delete_namespaced_ingress(name=name, namespace=namespace)

        log.debug("Deleted ingress resource: %s/%s" % (namespace, name))

    except (ApiException, HTTPError) as exc:
        if not isinstance(exc, ApiException) or exc.status != 404:
            log.error("Error deleting ingress resource: %s" % str(exc))
            raise exc


# Include workbench app labels
# Example:      'labels': {'manager': 'workbench',
#                          'pod-template-hash': '977967b76',
#                          'user': 'test',
#                          'workbench-app': 's00402'}
def get_pod_name(user, ssid):
    namespace = get_resource_namespace(username=user)
    id_segments = ssid.split('-')

    userapp_id = id_segments[0]
    service_key = id_segments[1]

    print(f"Looking up pod for user={user} for ssid={ssid} within userapp={userapp_id}")

    v1 = client.CoreV1Api()
    ret = v1.list_namespaced_pod(namespace=namespace,
                                 label_selector='manager=%s,user=%s,workbench-app=%s,workbench-svc=%s' %
                                                ('workbench', user, userapp_id, service_key))
    if len(ret.items) > 1:
        print(
            f"Warning: {len(ret.items)} matches found for user={user} for ssid={ssid} within userapp={userapp_id}. Assuming first Running/Ready pod.")
    elif len(ret.items) == 0:
        print(f"Warning: no matches found for user={user} for ssid={ssid} within userapp={userapp_id}")
    print("Searching...")
    print("Searching...")
    print("Searching...")
    print("Searching...")

    for i in ret.items:
        print(i.metadata.name)
        return i.metadata.name

    raise Exception("Failed to find pod name for: " + user + "/" + ssid)
    # return { 'name': ingress_name, 'namespace': ingress_namespace }, 201


def get_pods():
    v1 = client.CoreV1Api()
    print("Listing pods with their IPs:")
    ret = v1.list_pod_for_all_namespaces(watch=False)
    for i in ret.items:
        print("%s\t%s\t%s" % (i.status.pod_ip, i.metadata.namespace, i.metadata.name))
    # return { 'name': ingress_name, 'namespace': ingress_namespace }, 201


#
#
#
# Kubernetes Security
#
#
#
def create_network_policy(policy_name, **kwargs):
    networkingv1 = client.NetworkingV1Api()

    policy_namespace = kwargs['namespace'] if 'namespace' in kwargs else 'default'
    policy_labels = kwargs['labels'] if 'labels' in kwargs else {}
    policy_annotations = kwargs['annotations'] if 'annotations' in kwargs else {}

    match_labels = kwargs['match_labels'] if 'match_labels' in kwargs else {}
    match_expressions = kwargs['match_expressions'] if 'match_expressions' in kwargs else []

    egress = kwargs['egress'] if 'egress' in kwargs else []
    ingress = kwargs['ingress'] if 'ingress' in kwargs else []

    egress_len = len(egress)
    ingress_len = len(ingress)
    policy_types = \
        ["Ingress", "Egress"] if ingress_len > 0 and egress_len > 0 else \
            ["Ingress"] if ingress_len > 0 and egress_len == 0 else \
                ["Egress"] if egress_len > 0 and ingress_len == 0 else \
                    []

    if not policy_types:
        log.warning("Warning: Creating NetworkPolicy with empty policy_types")

    if not match_labels and not match_expressions:
        log.warning("Warning: Creating NetworkPolicy that would operate on all resources")

    policy = networkingv1.create_namespaced_network_policy(namespace=policy_namespace, body=client.V1NetworkPolicy(
        api_version='v1',
        kind='NetworkPolicy',
        metadata=client.V1ObjectMeta(
            name=policy_name,
            namespace=policy_namespace,
            annotations=policy_annotations,
            labels=policy_labels
        ),
        spec=client.V1NetworkPolicySpec(
            egress=egress,
            ingress=ingress,
            policy_types=policy_types,
            pod_selector=client.V1LabelSelector(
                match_expressions=match_expressions,
                match_labels=match_labels
            )
        )
    )
                                                           )
    return policy


def delete_network_policy(name, **kwargs):
    networkingv1 = client.NetworkingV1Api()
    namespace = kwargs['namespace'] if 'namespace' in kwargs else 'default'

    try:
        networkingv1.delete_namespaced_network_policy(namespace=namespace, name=name)
        log.debug("Deleted networkpolicy resource: %s/%s" % (namespace, name))
    except (ApiException, HTTPError) as exc:
        if not isinstance(exc, ApiException) or exc.status != 404:
            log.error("Error deleting networkpolicy resource: %s" % str(exc))
            raise exc


def create_resource_quota(quota_name, hard_quotas, **kwargs):
    v1 = client.CoreV1Api()

    quota_namespace = kwargs['namespace'] if 'namespace' in kwargs else 'default'
    quota_labels = kwargs['labels'] if 'labels' in kwargs else {}
    quota_annotations = kwargs['annotations'] if 'annotations' in kwargs else {}

    try:
        quota = v1.create_namespaced_resource_quota(quota_namespace, body=client.V1ResourceQuota(
            api_version='v1',
            kind='ResourceQuota',
            metadata=client.V1ObjectMeta(
                name=quota_name,
                namespace=quota_namespace,
                annotations=quota_annotations,
                labels=quota_labels
            ),
            spec=client.V1ResourceQuotaSpec(
                scope_selector=client.V1ScopeSelector(
                    match_expressions=[
                        # TODO: Selector for namespace and/or user labels
                        # client.V1ScopedResourceSelectorRequirement(
                        # scop
                        # )
                    ]
                ),
                hard=hard_quotas
            )
        ))
        return quota
    except (ApiException, HTTPError) as exc:
        if not isinstance(exc, ApiException) or exc.status != 409:
            log.error("Error creating networkpolicy resource: %s" % str(exc))
            raise exc


#
#
#
# V2 / CRD Stuff below here
#
#
#
def list_custom_app_specs():
    custom = client.CustomObjectsApi()
    return custom.list_cluster_custom_object(CRD_GROUP_NAME, CRD_VERSION_V1, CRD_APPSPECS_PLURAL)


def create_custom_app_spec(app_spec):
    custom = client.CustomObjectsApi()
    return custom.create_cluster_custom_object(
        group=CRD_GROUP_NAME,
        plural=CRD_APPSPECS_PLURAL,
        version=CRD_VERSION_V1,
        body={
            "apiVersion": "%s/%s" % (CRD_GROUP_NAME, CRD_VERSION_V1),
            "kind": CRD_APPSPECS_KIND,
            "metadata": {"name": "mongo"},
            "spec": app_spec,
        }
    )


def retrieve_custom_app_spec(key):
    custom = client.CustomObjectsApi()
    return custom.get_cluster_custom_object(group=CRD_GROUP_NAME,
                                            version=CRD_VERSION_V1,
                                            plural=CRD_APPSPECS_PLURAL,
                                            name=key)['spec']


def replace_custom_app_spec(key, app_spec):
    custom = client.CustomObjectsApi()
    return custom.replace_cluster_custom_object(CRD_GROUP_NAME, CRD_VERSION_V1, CRD_APPSPECS_PLURAL,
                                                name=key, body=app_spec)


def delete_custom_app_spec(key):
    custom = client.CustomObjectsApi()
    return custom.delete_cluster_custom_object(CRD_GROUP_NAME, CRD_VERSION_V1, CRD_APPSPECS_PLURAL,
                                               name=key)


def list_custom_user_apps(namespace):
    custom = client.CustomObjectsApi()
    return custom.list_namespaced_custom_object(CRD_GROUP_NAME, CRD_VERSION_V1, namespace, CRD_USERAPPS_PLURAL)
    # return custom.list_namespaced_custom_object(group=CRD_GROUP_NAME, version=CRD_VERSION_V1,
    #                                            plural=CRD_USERAPPS_PLURAL, namespace=namespace)


def create_custom_user_app(stack, namespace):
    custom = client.CustomObjectsApi()
    return custom.create_namespaced_custom_object(
        group=CRD_GROUP_NAME,
        plural=CRD_USERAPPS_PLURAL,
        namespace=namespace,
        version=CRD_VERSION_V1,
        body={
            "apiVersion": "%s/%s" % (CRD_GROUP_NAME, CRD_VERSION_V1),
            "kind": CRD_USERAPPS_KIND,
            "metadata": {
                "name": stack['id'],
                "namespace": namespace
            },
            "spec": stack,
        }
    )


def retrieve_custom_user_app(name, namespace):
    custom = client.CustomObjectsApi()
    return custom.get_namespaced_custom_object(CRD_GROUP_NAME, CRD_VERSION_V1, CRD_USERAPPS_KIND,
                                               name=name, namespace=namespace)['spec']


def replace_custom_user_app(name, namespace, user_app):
    custom = client.CustomObjectsApi()
    return custom.replace_namespaced_custom_object(CRD_GROUP_NAME, CRD_VERSION_V1, CRD_USERAPPS_KIND,
                                                   name=name, namespace=namespace,
                                                   body=user_app)


def delete_custom_user_app(name, namespace):
    custom = client.CustomObjectsApi()
    return custom.delete_namespaced_custom_object(CRD_GROUP_NAME, CRD_VERSION_V1, CRD_USERAPPS_KIND,
                                                  name=name, namespace=namespace)
