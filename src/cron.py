import sys
import os
from global_vars import STATUS_OK, config, log
from dbconnector import DbConnector
from jobutils import construct_job_object
from jobutils import delete_job_files
from datetime import timedelta, datetime
from kubernetes.client.rest import ApiException

# Get global instance of the job handler database interface
db = DbConnector(
    mysql_host=config['db']['host'],
    mysql_user=config['db']['user'],
    mysql_password=config['db']['pass'],
    mysql_database=config['db']['database'],
)

# Load Kubernetes API
try:
    import kubejob
except Exception as e:
    log.warning(f'''Failure loading Kubernetes client: {e}''')
    sys.exit(0)

def job_expiration():
    '''Delete jobs older than 48 hours.'''
    ## Scan job table for all jobs marked as deleted
    job_list = db.select_job_records(fields=['job_id', 'time_created'])
    for job in job_list:
        try:
            time_now = datetime.utcnow()
            job_id = job['job_id']
            time_created = job['time_created']
            # log.debug(f'''time_created: {time_created.strftime('%Y%m%d-%H%M%S')}''')
            time_to_delete = time_created + timedelta(hours = 48)
            # log.debug(f'''time_to_delete: {time_to_delete.strftime('%Y%m%d-%H%M%S')}''')
            if time_now > time_to_delete:
                log.debug(f'''Expiring job files for job "{job_id}" [{time_created.strftime('%Y%m%d-%H%M%S')}]''')
                try:
                    ## Mark job deleted in `job` table
                    db.mark_job_deleted(job_id)
                except Exception as e:
                    log.error(f'''Error marking job "{job_id}" deleted: {e}''')
                try:
                    ## Delete Kubernetes objects associated with the job
                    kubejob.delete_job(job_id=job_id)
                except ApiException as e:
                    ## No need to raise exception for 404 not found errors. Assume this is the error.
                    pass
                except Exception as e:
                    log.error(f'''Error deleting Kubernetes Job "{job_id}" objects: {e}''')
                try:
                    ## Delete files associated with the job
                    assert delete_job_files(job_id)
                except Exception as e:
                    log.error(f'''Error deleting job "{job_id}" files: {e}''')
        except Exception as e:
            log.error(f'''Error expiring job files ({job_id}): {e}''')

def garbage_collection():
    '''Remove job files on disk and orphaned Kubernetes Job and ConfigMap objects
    associated with jobs marked as deleted in the job table. Ideally this will 
    never take any action, because these resources should have been deleted when
    the job was marked for deletion; however, there may be instances in which the
    original deletions fail.'''
    ## Scan job table for all jobs marked as deleted
    job_list = db.select_job_records(deleted=True, fields=['job_id'])
    ## Delete any files on disk associated with the deleted jobs
    for job in job_list:
        try:
            assert delete_job_files(job['job_id'])
        except Exception as e:
            log.error(f'''Error deleting job files ({job['job_id']}): {e}''')
    ## List all existing Kubernetes Job objects
    results = kubejob.list_jobs()
    if not results['jobs']:
        return
    kube_jobs = []
    for job_info in results['jobs']:
        kube_jobs.append(construct_job_object(job_info))
    ## Identify orphaned Kubernetes Jobs and delete them
    for kube_job in kube_jobs:
        matched = [job for job in job_list if kube_job['jobId'] == job['job_id']]
        if matched:
            try:
                ## Delete Kubernetes objects associated with the job
                kubejob.delete_job(job_id=kube_job['jobId'])
            except ApiException as e:
                ## No need to raise exception for 404 not found errors. Assume this is the error.
                pass
            except Exception as e:
                log.error(f'''Error deleting Kubernetes Job objects: {e}''')

def update_job_status(user_id: str = '', job_id: str = ''):
    '''Update the information in the job table with the current state of the Kubernetes
    Jobs'''
    ## Scan job table for all jobs marked as still processing
    job_list = []
    for phase in ['pending', 'queued', 'executing']:
        if job_id:
            job_list.extend(db.select_job_records(job_id=job_id, phase=phase))
        elif user_id:
            job_list.extend(db.select_job_records(user_id=user_id, phase=phase))
        else:
            job_list.extend(db.select_job_records(phase=phase))
    # log.debug(yaml.dump(job_list, indent=2))
    ## List all existing Kubernetes Job objects
    results = kubejob.list_jobs()
    if not results['jobs']:
        return
    kube_jobs = []
    for job_info in results['jobs']:
        kube_jobs.append(construct_job_object(job_info))
    matched_jobs = []
    for job in job_list:
        ## Identify missing Kubernetes Jobs
        found = [kube_job for kube_job in kube_jobs if kube_job['jobId'] == job['job_id']]
        if not found:
            ## Mark the job status as error
            db.update_job(job_id=job['job_id'], phase='error')
            ## TODO: check for dangling ConfigMap object and delete it
        else:
            matched_jobs.append(job)
    ## For the jobs with a matching Kubernetes Job object, update the job table with
    ## the latest job status
    for kube_job in kube_jobs:
        ## Search for matching job in the the job table
        match = [job for job in matched_jobs if job['job_id'] == kube_job['jobId']]
        if match:
            job = match[0]
            try:
                db.update_job(
                    job_id=job['job_id'],
                    phase=kube_job['phase'],
                    start_time=kube_job['startTime'],
                    end_time=kube_job['endTime'],
                )
            except Exception as e:
                log.error(f'''Error updating job "{job['job_id']}": {e}''')

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Execute periodic functions required by the API server.')
    parser.add_argument('--all', action='store_true', help='Run all cron functions.')
    parser.add_argument('--status', action='store_true', help='Update job status.')
    parser.add_argument('--garbage', action='store_true', help='Perform garbage collection.')
    parser.add_argument('--expire', action='store_true', help='Expire jobs.')
    args = parser.parse_args()

    ## TODO: Make database queries more efficient. For example, query Kubernetes API server for
    ##       Job objects once and pass this list to cron functions that need it. Ideally this 
    ##       list will always be small because of the `ttlSecondsAfterFinished` setting in the 
    ##       Job object configuration.
    ## Execute all cron tasks
    if args.all or args.status:
        log.info('Updating job status...')
        update_job_status()
    if args.all or args.garbage:
        log.info('Running garbage collection...')
        garbage_collection()
    if args.all or args.expire:
        log.info('Expiring jobs...')
        job_expiration()
