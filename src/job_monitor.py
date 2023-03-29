import os
import time
import requests
from requests.exceptions import Timeout
from global_vars import log, config
import shutil
import email_utils
from dbconnector import DbConnector

db = DbConnector(
    mysql_host=config['db']['host'],
    mysql_user=config['db']['user'],
    mysql_password=config['db']['pass'],
    mysql_database=config['db']['database'],
)

def report_job_ended(url='', job_id='', token='', phase='completed', numAttempts=5):
    assert job_id and url
    ## Make several attempts to call the API, tolerating timeouts and other errors
    attemptIdx = 0
    while attemptIdx < numAttempts:
        attemptIdx += 1
        try:
            response = requests.request('POST', f'''{url}/end/{job_id}''', timeout=2,
                json={
                    'token': token,
                    'phase': phase,
                }
            )
            try:
                assert response.status_code in [200, 204]
                log.debug(f'''Sucessfully reported job "{job_id}" (finished/error). Please check the log file for futher details.''')
                return
            except:
                log.error(f'''Error reporting job "{job_id}" finished: [{response.status_code}] {response.text}''')
                return
        except Timeout:
            log.warning(f'''Timeout posting that job "{job_id}" finished. (Attempt {attemptIdx-1}/{numAttempts})''')
    return

def send_job_status_email(url='', job_id='', phase='completed'):
    assert job_id and url
    ## Make several attempts to call the API, tolerating timeouts and other errors
    try:
        ## Query the specified job by ID
        # update_job_status(job_id=job_id)
        valid_properties = {
            'phase': 'phase',
            'user_id': 'results',
            'command': 'command',
            'run_id': 'run_id',
            'email': 'email',
            'type': 'type',
            'job_info': 'job_info',
        }
        fields = ['job_id', 'phase', 'time_created', 'email']
        job_list = db.select_job_records(job_id=job_id, fields=fields)
        try:
            job = job_list[0]
        except:
            job = {}
        ## TODO: Implement the specific property return if requested
        ## If a specific job property was requested using an API endpoint
        ## of the form `/job/[job_id]/[property]]`, return that property only.
        if property and property in valid_properties.keys():
            job = job[valid_properties[property]]
        if phase == "completed":
            email_utils.send_email(job['email'], f'''CLEAN Job {job_id} result ready''', f'''CLEAN Job result available at https://clean.frontend.mmli1.ncsa.illinois.edu/results/{job_id}''')
        else:
            email_utils.send_email(job['email'], f'''CLEAN Job {job_id} failed''', f'''An error occurred in computing the result for the CLEAN job.''')
        return
    except Exception as e:
        log.error(f'''Error sending email for job "{job_id}".''')
        return 

def main(args):
    job_id = args.job
    token = 'dummy'
    url = args.url
    files_dir = args.dir
    assert job_id and url
    
    ## If prestop lifecycle hook was triggered by Kubernetes, report failed job.
    if args.prestop:
        report_job_ended(url=url, job_id=job_id, token=token, phase='aborted')
        return
    
    try:
        log.debug(f'''URL: "{url}"''')
        response = requests.request('POST', f'''{url}/start/{job_id}''', timeout=3,
            json={
                'token': token,
            }
        )
        try:
            assert response.status_code in [200, 204]
            log.debug(f'''Job "{job_id}" start reported successfully.''')
        except:
            log.error(f'''Job "{job_id}" start report failed: [{response.status_code}] {response.text}''')
    except Timeout:
        log.warning(f'''Timeout posting job started.''')
    
    ## Watch the filesystem for the file that indicates the job is complete
    finished_file_path = os.path.join(files_dir, 'finished')
    error_file_path = os.path.join(files_dir, 'error')
    log.debug(f'''[{job_id}] Watching for file "{finished_file_path}" and "{error_file_path}"''')
    # Monitor checks for the presence of "finished" or "error" file
    while True:
        if os.path.isfile(finished_file_path):
            # Presence of finished file means that the job has finished executing
            report_job_ended(url=url, job_id=job_id, token=token, phase='completed')
            send_job_status_email(url=url, job_id=job_id, phase='completed')
            return 0
        elif os.path.isfile(error_file_path):
            # Presence of error file means the job had some exception
            report_job_ended(url=url, job_id=job_id, token=token, phase='failed')
            send_job_status_email(url=url, job_id=job_id, phase='failed')
            return 1
        time.sleep(20)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description='Launch jobs to test job system.')
    parser.add_argument('--job', required=True, nargs='?',
                        default='', type=str, help='job ID')
    parser.add_argument('--token', required=True, nargs='?',
                        default='', type=str, help='API auth token')
    parser.add_argument('--url', required=True, nargs='?',
                        default='', type=str, help='API endpoint URL')
    parser.add_argument('--dir', required=True, nargs='?',
                        default='', type=str, help='path to job files')
    parser.add_argument('--prestop', action='store_true',
                        help='Report prematurely aborted job.')

    args = parser.parse_args()
    main(args)
