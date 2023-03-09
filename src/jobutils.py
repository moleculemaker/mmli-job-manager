import os
from global_vars import STATUS_OK, config, log
from dbconnector import DbConnector
from kubejob import get_job_root_dir_from_id
from shutil import rmtree

# Get global instance of the job handler database interface
db = DbConnector(
    mysql_host=config['db']['host'],
    mysql_user=config['db']['user'],
    mysql_password=config['db']['pass'],
    mysql_database=config['db']['database'],
)


def valid_job_id(job_id):
    # For testing purposes, treat the string 'invalid_job_id' as an invalid job_id
    return isinstance(job_id, str) and len(job_id) > 0


def construct_job_object(job_info):
    job = {}
    try:
        creationTime = job_info['creation_time']
        startTime = job_info['status']['start_time']
        endTime = job_info['status']['completion_time']
        destructionTime = None  # TODO: Should we track deletion time?
        try:
            executionDuration = (endTime - startTime).total_seconds()
        except:
            executionDuration = None
        try:
            message = job_info['message']
        except:
            message = ''
        # Determine job phase. For definitions see:
        #     https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#ExecutionPhase
        ## PENDING: the job is accepted by the service but not yet committed for execution by the client.
        ## QUEUED: the job is committed for execution by the client but the service has not yet assigned it to a processor.
        ## Technically the startTime value should indicate if the QUEUED phase has been reached; however
        ## its initial value is random due to variation in timing when polling the Kubernetes API server.
        ## Given that we are not polling the API server again to update this status, to reduce confusion
        ## we will simply assume that the job has been QUEUED.
        job_phase = 'queued'
        if startTime:
            # job_phase = 'queued'
            # if job_info['status']['active']:
            #     job_phase = 'executing'
            if job_info['status']['failed']:
                job_phase = 'error'
        if endTime:
            job_phase = 'completed'
            if not job_info['status']['succeeded'] or job_info['status']['failed']:
                job_phase = 'error'

        results = []
        try:
            for idx, filepath in enumerate(job_info['output_files']):
                results.append({
                    'id': idx,
                    'uri': filepath,
                    # 'mime-type': 'image/fits',
                    # 'size': '3000960',
                })
        except Exception as e:
            log.error(str(e))
            results = []
        # See job_schema.xml
        #   https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#jobobj
        job = {
            'jobId': job_info['job_id'],
            'runId': job_info['run_id'],
            'ownerId': job_info['owner_id'],
            'phase': job_phase,
            'creationTime': creationTime,
            'startTime': startTime,
            'endTime': endTime,
            'executionDuration': executionDuration,
            'destruction': destructionTime,
            'parameters': {
                'command': job_info['command'],
                'environment': job_info['environment'],
            },
            'results': results,
            'errorSummary': {
                'message': message,
            },
            'jobInfo': {
            },
        }
    except Exception as e:
        log.error(str(e))
    return job


def delete_job_files(job_id: str) -> bool:
    try:
        delete_path = get_job_root_dir_from_id(job_id)
        if os.path.isdir(delete_path):
            rmtree(delete_path)
        return True
    except Exception as e:
        log.error(f'''Error deleting job "{job_id}" file path "{delete_path}": {e}''')
        return False
