import json
from global_vars import config, log
from dbconnector import DbConnector

# Get global instance of the job handler database interface
db = DbConnector(
    mysql_host=config['db']['host'],
    mysql_user=config['db']['user'],
    mysql_password=config['db']['pass'],
    mysql_database=config['db']['database'],
)


def update_job_stats():
    num_total = 0
    num_complete = 0
    num_error = 0
    num_aborted = 0
    total_duration = 0
    jobs_with_duration = 0
    jobs_per_user = {}
    user_agents = {}
    
    job_list = db.select_job_records(fields=['time_start', 'time_end', 'phase', 'user_id', 'user_agent'])
    num_total = len(job_list)
    for job in job_list:
        user_id = job['user_id']
        user_agent = job['user_agent']
        phase = job['phase']
        end_time = job['time_end']
        start_time = job['time_start']
        ## Job Status
        if phase == 'completed':
            num_complete += 1
        elif phase == 'aborted':
            num_aborted += 1
        elif phase == 'error':
            num_error += 1
        ## Job Duration
        if end_time and start_time and end_time > start_time:
            total_duration += (end_time - start_time).total_seconds()
            jobs_with_duration += 1
        ## User distribution
        if user_id not in jobs_per_user:
            jobs_per_user[user_id] = 1
        else:
            jobs_per_user[user_id] += 1
        ## User agent
        if user_agent not in user_agents:
            user_agents[user_agent] = 1
        else:
            user_agents[user_agent] += 1
        
    ## Calculate the average job duration
    avg_duration = round(total_duration/jobs_with_duration)
    ## Calculate the percentage of total jobs per user
    users = db.select_registrants(fields=['name', 'user_id'])
    dist_users = []
    for user_id, num_jobs in jobs_per_user.items():
        name = [user['name'] for user in users if user['user_id'] == user_id]
        if name:
            name = name[0]
        else:
            name = ''
        dist_users.append({
            'user_id': user_id,
            'name': name,
            'percentage': round(100*num_jobs/num_total, 2),
        })
    dist_users_str = json.dumps(dist_users)
    user_agents_str = json.dumps(user_agents)
    db.insert_stats_jobs({
        'num_total': num_total,
        'num_complete': num_complete,
        'num_error': num_error,
        'num_aborted': num_aborted,
        'avg_duration': avg_duration,
        'num_users': len(dist_users),
        'dist_users': dist_users_str,
        'dist_user_agents': user_agents_str,
    })


def main(args):
    if args.all or args.jobs:
        log.info('Updating job stats...')
        update_job_stats()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description='Update statistics about service usage.')
    parser.add_argument('--all', action='store_true',
                        help='Run all stats updates.')
    parser.add_argument('--jobs', action='store_true',
                        help='Update job stats.')
    args = parser.parse_args()
    main(args)
