import os
import time
import requests
from requests.exceptions import Timeout
from global_vars import log, config
import shutil


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
                log.debug(f'''Sucessfully reported job "{job_id}" finished.''')
                return
            except:
                log.error(f'''Error reporting job "{job_id}" finished: [{response.status_code}] {response.text}''')
                return
        except Timeout:
            log.warning(f'''Timeout posting that job "{job_id}" finished. (Attempt {attemptIdx-1}/{numAttempts})''')
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
    ##
    finished_file_path = os.path.join(files_dir, 'finished')
    log.debug(f'''[{job_id}] Watching for file "{finished_file_path}"...''')
    while True:
        if os.path.isfile(finished_file_path):
            report_job_ended(url=url, job_id=job_id, token=token, phase='completed')
            return 0
        time.sleep(5)

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
