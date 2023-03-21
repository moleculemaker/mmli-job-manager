import os
import sys
import requests
import time
import yaml
import secrets
from datetime import datetime
from requests.exceptions import Timeout

# try:
#     assert os.environ['SPT_API_TOKEN']
# except:
#     print(f'''Export an environment variable SPT_API_TOKEN containing your API token prior to executing this script:
#     export SPT_API_TOKEN="eyJ0eXAiO...f72LVU"
#     python {sys.argv[0]}
#     ''')
#     sys.exit(1)


CONFIG = {
    # 'auth_token': os.environ['SPT_API_TOKEN'],
    # 'apiBaseUrl': 'http://localhost:8888/api/v1',
    'apiBaseUrl': ' https://jobmgr.mmli1.ncsa.illinois.edu/api/v1',
}


def submit_job(payload: str = '', environment: list = [],) -> dict:
    job_info = {}
    """Submits a MMLI job and returns the complete server response which includes the job ID."""
    ## Validate inputs
    data = {
        'input_fasta': payload,
    }
    if environment:
        data['environment'] = environment
    ## Submit job
    response = requests.request('POST',
        f'''{CONFIG['apiBaseUrl']}/job/submit''',
        # headers={'Authorization': f'''Bearer {CONFIG['auth_token']}'''},
        json=data,
    )
    try:
        assert response.status_code in [200, 204]
        job_info = response.json()
    except:
        print(f'''[{response.status_code}] {response.text}''')
        return job_info
    return job_info


def get_job_status(job_id: str = '') -> list:
    """Get status of individual job or all jobs belonging to the authenticated user."""
    job_info = {}
    ## Validate inputs
    assert isinstance(job_id, str) or not job_id
    url = f'''{CONFIG['apiBaseUrl']}/job/status'''
    # if job_id:
    #     url += f'''/{job_id}'''
    ## Fetch job status
    try:
        response = requests.request('POST',
            url,
            params={
                'jobId': job_id,
            },
            # headers={'Authorization': f'''Bearer {CONFIG['auth_token']}'''},
            timeout=3,
        )
    except Timeout:
        print(f'''ERROR: Timeout fetching job status''')
        return job_info
    try:
        assert response.status_code in [200, 204]
        job_info = response.json()
    except:
        print(f'''[{response.status_code}] {response.text}''')
        return job_info
    return job_info


def delete_job(job_id: str = '') -> list:
    """Delete individual job"""
    ## Validate inputs
    assert isinstance(job_id, str) and job_id
    ## Delete job
    try:
        response = requests.request('DELETE',
            f'''{CONFIG['apiBaseUrl']}/uws/job/{job_id}''',
            # headers={'Authorization': f'''Bearer {CONFIG['auth_token']}'''},
        )
    except Timeout:
        print(f'''ERROR: Timeout requesting job deletion''')
        return False
    try:
        assert response.status_code in [200, 204]
        return True
    except:
        print(f'''[{response.status_code}] {response.text}''')
        return False


def job_status_poll(job_id):
    print(f'Polling status of job "{job_id}"...', end='')
    job_info = {}
    while not job_info or ('phase' in job_info and job_info['phase'] in ['pending', 'queued', 'executing']):
        print('.', end='', sep='', flush=True)
        # Fetch the current job status
        job_info = get_job_status(job_id)
        # print(json.dumps(job_info))
        time.sleep(3)
    print('\n')
    return job_info

def main():
    
    import argparse

    parser = argparse.ArgumentParser(description='Launch jobs to test job system.')
    parser.add_argument('--num', nargs='?', type=int, default=0, help='number of jobs to launch')
    parser.add_argument('--poll', action='store_true')
    parser.add_argument('--delete', action='store_true')
    args = parser.parse_args()
    
    ## Delete all jobs
    ##
    if args.delete:
        jobs = get_job_status()
        for job_info in jobs:
            print(f'''Deleting {job_info['job_id']}...''')
            delete_job(job_info['job_id'])
        sys.exit(0)

    ## Submit new jobs
    ##
    job_ids = []
    job_idx = 0
    while job_idx < args.num:
        ## Select a random job config
        fileIdx = secrets.choice(range(1, 6))
        # fileIdx = 1
        fileName = f'''job_config.{fileIdx}.yaml'''
        # fileName = f'''job_config.yaml'''
        
        ## Load job configuration
        ##
        with open(fileName, "r") as conf_file:
            job_config = yaml.load(conf_file, Loader=yaml.FullLoader)
        # print(yaml.dump(job_config, indent=2))
        
        ## Submit new MMLI job
        ##
        print(f'Submitting new MMLI job ({fileName})...')
        job_info = submit_job(
            payload=job_config['input_fasta'],
            environment=[{'name': 'LOG_LEVEL', 'value': 'DEBUG'}]
        )
        if not job_info:
            return
        job_id = job_info['jobId']
        print(f'Job "{job_id}" created.')
        job_ids.append(job_id)
        job_idx += 1
    
    ## Poll status of jobs
    ##
    if args.poll:
        num_jobs = -1
        num_incomplete = -1
        while num_jobs < 0 or num_incomplete != 0:
            num_complete = 0
            num_incomplete = 0
            ## Poll the status of the jobs
            ##
            print(f'''[{datetime.now().strftime('%H:%M:%S')}] Polling job status...''')
            for job_id in job_ids:
                job_status = get_job_status(job_id=job_id)
                if not job_status:
                    print(f'''[{datetime.now().strftime('%H:%M:%S')}] ERROR fetching job status.''')
                    num_incomplete = -1
                    break
                else:
                    num_jobs = len(job_ids)
                    job_info = job_status
                    phase = job_info['status']
                    if phase in ['completed', 'aborted', 'error']:
                        num_complete += 1
                    else:
                        num_incomplete += 1
            print(f'''[{datetime.now().strftime('%H:%M:%S')}] {num_complete}/{num_jobs} jobs complete.''')
            time.sleep(5)


if __name__ == "__main__":
    main()
