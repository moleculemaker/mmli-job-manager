import sys
import requests
import logging

# Configure logging
logging.basicConfig(
    format='%(asctime)s [%(name)-12s] %(levelname)-8s %(message)s',
)
log = logging.getLogger(__name__)
try:
    log.setLevel(config['server']['logLevel'].upper())
except:
    log.setLevel('DEBUG')

def run_monitor_job():
    try:
        log.debug(f'''Running the monitor!''')
    except Exception as e:
        log.error(f'''Error expiring job files ({job_id}): {e}''')

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Execute periodic functions required by the API server.')
    parser.add_argument('--monitor', action='store_true', help='Run monitor job.')
    args = parser.parse_args()

    if args.monitor:
        log.info('Running monitor...')
        run_monitor_job()
