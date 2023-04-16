import os
import global_vars
from global_vars import STATUS_OK, config, log
import tornado.ioloop
import tornado.web
import tornado.template
import tornado.escape
import json
from datetime import datetime
import pytz
import bcrypt
import time
from dbconnector import DbConnector
import re
from mimetypes import guess_type
from jobutils import valid_job_id, construct_job_object
import email_utils
import requests
from requests.exceptions import Timeout

# Get global instance of the job handler database interface
db = DbConnector(
    mysql_host=config['db']['host'],
    mysql_user=config['db']['user'],
    mysql_password=config['db']['pass'],
    mysql_database=config['db']['database'],
)

APPCONFIG = {
    # 'auth_token': os.environ['SPT_API_TOKEN'],
    'baseUrl': 'https://clean.frontend.mmli1.ncsa.illinois.edu',
}
utc_timezone = pytz.timezone('UTC')

log.info(f"Now starting API server: mysql://{config['db']['user']}:****@{config['db']['host']}:3306/{config['db']['database']}")

# Load Kubernetes API
try:
    import kubejob
except Exception as e:
    log.warning(f'''Failure loading Kubernetes client: {e}''')


class BaseHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers",
                        "Origin, X-Requested-With, Content-Type, Accept, Authorization")
        self.set_header("Access-Control-Allow-Methods",
                        " POST, PUT, DELETE, OPTIONS, GET")

    def options(self):
        self.set_status(204)
        self.finish()

    def get_current_user(self):
        return self.get_secure_cookie("user")

    def getarg(self, arg, default=None):
        '''
        Calls to this function in BaseHandler.get(), BaseHandler.post(), etc must be surrounded by try/except blocks like so:

            try:
                ownerId = self.getarg('ownerId')
            except:
                self.finish()
                return

        '''
        value = default
        try:
            # If the request encodes arguments in JSON, parse the body accordingly
            if 'Content-Type' in self.request.headers and self.request.headers['Content-Type'] in ['application/json', 'application/javascript']:
                data = tornado.escape.json_decode(self.request.body)
                if default == None:
                    # The argument is required and thus this will raise an exception if absent
                    value = data[arg]
                else:
                    # Set the value to the default
                    value = default if arg not in data else data[arg]
            # Otherwise assume the arguments are in the default content type
            else:
                # The argument is required and thus this will raise an exception if absent
                if default == None:
                    value = self.get_argument(arg)
                else:
                    value = self.get_argument(arg, default)
        except Exception as e:
            response = str(e).strip()
            log.error(response)
            # 400 Bad Request: The server could not understand the request due to invalid syntax.
            # The assumption is that if a function uses `getarg()` to get a required parameter,
            # then the request must be a bad request if this exception occurs.
            self.send_response(response, http_status_code=global_vars.HTTP_BAD_REQUEST, return_json=False)
            raise e
        return value

    # The datetime type is not JSON serializable, so convert to string
    def json_converter(self, o):
        if isinstance(o, datetime):
            return o.__str__()

    def send_response(self, data='', http_status_code=global_vars.HTTP_OK, return_json=True, indent=None):
        if return_json:
            if indent:
                self.write(json.dumps(data, indent=indent, default=self.json_converter))
            else:
                self.write(json.dumps(data, default=self.json_converter))
            self.set_header('Content-Type', 'application/json')
        else:
            self.write(data)
        self.set_status(http_status_code)

    def is_valid_email(self, email):
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return re.match(pattern, email) is not None

def escape_html(htmlstring):
    escapes = {'\"': '&quot;',
               '\'': '&#39;',
               '<': '&lt;',
               '>': '&gt;'}
    # This is done first to prevent escaping other escapes.
    htmlstring = htmlstring.replace('&', '&amp;')
    for seq, esc in escapes.items():
        htmlstring = htmlstring.replace(seq, esc)
    return htmlstring


def get_api_base_path():
    ## Generate API base path for links
    basePath = f'''{config['server']['basePath']}/{config['server']['apiBasePath']}'''
    basePath = basePath.strip('/')
    return basePath


class JobReportStartHandler(BaseHandler):
    def post(self, job_id=None):
        try:
            assert valid_job_id(job_id)
        except Exception as e:
            self.send_response("Invalid job ID", http_status_code=global_vars.HTTP_BAD_REQUEST, return_json=False)
            self.finish()
            return
        # try:
        #     token = self.getarg('token') # required
        # except:
        #     self.finish()
        #     return
        # try:
        #     assert token == config['jwt']['hs256Secret']
        # except:
        #     self.send_response("Invalid auth token", http_status_code=global_vars.HTTP_UNAUTHORIZED, return_json=False)
        #     self.finish()
        #     return
        try:
            log.debug(f'''Updating started job "{job_id}"...''')
            db.update_job(
                job_id=job_id,
                phase='executing',
                start_time=datetime.utcnow(),
            )
        except Exception as e:
            log.error(f'''Error updating job "{job_id}" start time: {e}''')


class JobReportCompleteHandler(BaseHandler):
    def post(self, job_id=None):
        try:
            assert valid_job_id(job_id)
        except Exception as e:
            self.send_response("Invalid job ID", http_status_code=global_vars.HTTP_BAD_REQUEST, return_json=False)
            self.finish()
            return
        try:
            # token = self.getarg('token') # required
            phase = self.getarg('phase', 'completed') # optional
        except:
            self.finish()
            return
        # try:
        #     assert token == config['jwt']['hs256Secret']
        # except:
        #     self.send_response("Invalid auth token", http_status_code=global_vars.HTTP_UNAUTHORIZED, return_json=False)
        #     self.finish()
        #     return
        try:
            log.debug(f'''Updating completed job "{job_id}"...''')
            db.update_job(
                job_id=job_id,
                phase=phase,
                end_time=datetime.utcnow(),
                queue_position=-1,
            )
        except Exception as e:
            log.error(f'''Error updating job "{job_id}": {e}''')
        try:
            ## Delete Kubernetes objects associated with the job if it was aborted
            if phase == 'aborted':
                kubejob.delete_job(job_id=job_id)
        except Exception as e:
            log.error(f'''Error deleting Kubernetes Job objects: {e}''')
        try:
            log.debug(f'''Querying job record "{job_id}"...''')
            job_query = db.select_job_records(job_id=job_id, hasEmailAddress=True)
            if not job_query:
                return
            job = job_query[0]
            if job['phase'] == "completed":
                email_utils.send_email(job['email'], f'''Result for your CLEAN Job ({job['job_id']}) is ready''', f'''The result for your CLEAN Job is available at {APPCONFIG['baseUrl']}/results/{job['job_id']}/1''')
            else:
                email_utils.send_email(job['email'], f'''CLEAN Job {job['job_id']} failed''', f'''An error occurred in computing the result for your CLEAN job.''')
            ## Remove the email address from the job record to mark as sent
            db.update_job(job_id=job_id, email='')
        except Exception as e:
            log.error(f'''Error sending job completion email "{job_id}": {e}''')



class JobHandler(BaseHandler):
    def put(self):
        try:
            echo = self.getarg('echo') # required

            job_config = {
                'echo': echo,
            }

            ## Command that the job container will execute. The `$JOB_OUTPUT_DIR` environment variable is
            ## populated at run time after a job ID and output directory have been provisioned.
            command = f'''echo {echo} | tee $JOB_OUTPUT_DIR/job.log'''
            log.debug(f"Job command: {command}")

            ## Options:

            ## environment is a list of environment variable names and values like [{'name': 'env1', 'value': 'val1'}]
            environment = self.getarg('environment', default=[]) # optional

            ## Number of parallel job containers to run. The containers will execute identical code. Coordination is the
            ## responsibility of the job owner.
            ## replicas = self.getarg('replicas', default=1) # optional
            replicas = 1

            ## Valid run_id value follows the Kubernetes label value constraints:
            ##   - must be 63 characters or less (cannot be empty),
            ##   - must begin and end with an alphanumeric character ([a-z0-9A-Z]),
            ##   - could contain dashes (-), underscores (_), dots (.), and alphanumerics between.
            ## See also:
            ##   - https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#runId
            ##   - https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set
            run_id = self.getarg('run_id', default='') # optional
            if run_id and (not isinstance(run_id, str) or run_id != re.sub(r'[^-._a-zA-Z0-9]', "", run_id) or not re.match(r'[a-zA-Z0-9]', run_id)):
                self.send_response('Invalid run_id. Must be 63 characters or less and begin with alphanumeric character and contain only dashes (-), underscores (_), dots (.), and alphanumerics between.', http_status_code=global_vars.HTTP_BAD_REQUEST, return_json=False)
                self.finish()
                return
            ## Obtain user ID from auth token
            # user_id = self._token_decoded["user_id"]
            user_id = 'DummyID'
        except Exception as e:
            self.send_response(
                str(e), http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return

        response = kubejob.create_job(
            command=command,
            run_id=run_id,
            owner_id=user_id,
            replicas=replicas,
            environment=environment,
            job_config=job_config,
        )
        log.debug(response)
        if response['status'] != global_vars.STATUS_OK:
            self.send_response(response['message'], http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return
        try:
            timeout = 30
            while timeout > 0:
                results = kubejob.list_jobs(
                    job_id=response['job_id'],
                )
                if results['jobs']:
                    job = construct_job_object(results['jobs'][0])
                    try:
                        user_agent = self.request.headers["User-Agent"]
                    except:
                        user_agent = ''
                    queue_position = 0
                    try:
                        ## TODO: Replace with specialized query that uses the SQL count() command
                        job_list = []
                        for phase in ['pending', 'queued', 'executing']:
                            job_list.extend(db.select_job_records(phase=phase, fields=['id']))
                        log.debug(job_list)
                        queue_position = len(job_list)
                    except Exception as e:
                        log.error(f'''Error querying job queue position: {e}''')
                    try:
                        command = job['parameters']['command']
                        if isinstance(command, list):
                            command = ' '.join(command)
                        ## TODO: Replace the JSON dump of the full job info with proper table records.
                        job_info = {
                            'job_id': job['jobId'],
                            'run_id': job['runId'],
                            'user_id': job['ownerId'],
                            'command': command,
                            'type': 'cutout',
                            'phase': job['phase'],
                            'time_created': job['creationTime'] or 0,
                            'time_start': job['startTime'] or 0,
                            'time_end': job['endTime'] or 0,
                            'user_agent': user_agent,
                            'email': 'dummy@example.com',
                            'job_info': json.dumps(job, default = self.json_converter),
                            'queue_position': queue_position,
                            }
                        db.insert_job_record(job_info)
                        ##
                        ## TODO: This is a hack for testing locally, because the monitor sidecar container
                        ##       would be calling back to the API server with a job start and end report.
                        ##
                        if not config['uws']['job']['monitorEnabled']:
                            log.debug(f'''Updating started job "{job_info['job_id']}"...''')
                            db.update_job(
                                job_id=job_info['job_id'],
                                phase='executing',
                                start_time=datetime.utcnow(),
                            )
                    except Exception as e:
                        err_msg = f'''Error recording job in database: {e}'''
                        log.error(err_msg)
                        self.send_response(err_msg, http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
                        self.finish()
                        return
                    self.send_response(job, indent=2)
                    self.finish()
                    return
                else:
                    timeout -= 1
                    time.sleep(0.300)
            self.send_response("Job creation timed out.", http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return
        except Exception as e:
            self.send_response(str(e), http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return

    def get(self, job_id=None, property=None):
        # See https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#resourceuri
        ## The valid properties are the keys of the returned job data structure
        valid_properties = {
            'phase': 'phase',
            'user_id': 'results',
            'command': 'command',
            'run_id': 'run_id',
            'email': 'email',
            'type': 'type',
            'job_info': 'job_info',
        }
        response = {}
        # If no job_id is included in the request URL, return a list of jobs. See:
        # UWS Schema: https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#UWSSchema
        if not job_id:
            phase = self.getarg('phase', default='') # optional
            if not phase or phase in global_vars.VALID_JOB_STATUSES:
                # results = kubejob.list_jobs()
                try:
                    ## Query for all jobs belonging to the authenticated user
                    # update_job_status(user_id=self._token_decoded["user_id"])
                    # job_list = db.select_job_records(user_id=self._token_decoded["user_id"], phase=phase)
                    job_list = db.select_job_records(user_id='DummyID', phase=phase)
                    self.send_response(job_list, indent=2)
                    self.finish()
                    return
                except Exception as e:
                    self.send_response(f'''Error querying job records: {e}''', http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
                    self.finish()
                    return
            else:
                response = 'Valid job categories are: {}'.format(global_vars.VALID_JOB_STATUSES)
                self.send_response(response, http_status_code=global_vars.HTTP_BAD_REQUEST, return_json=False)
                self.finish()
                return
        # If a job_id is provided but it is invalid, then the request is malformed:
        if not valid_job_id(job_id):
            self.send_response('Invalid job ID.', http_status_code=global_vars.HTTP_BAD_REQUEST, indent=2)
            self.finish()
            return
        # If a property is provided but it is invalid, then the request is malformed:
        elif isinstance(property, str) and property not in valid_properties:
            self.send_response(f'Invalid job property requested. Supported properties are {", ".join([key for key in valid_properties.keys()])}', http_status_code=global_vars.HTTP_BAD_REQUEST, indent=2)
            self.finish()
            return
        try:
            ## Query the specified job by ID
            # update_job_status(job_id=job_id)
            job_list = db.select_job_records(job_id=job_id)
            try:
                job = job_list[0]
            except:
                job = {}
            ## TODO: Implement the specific property return if requested
            ## If a specific job property was requested using an API endpoint
            ## of the form `/job/[job_id]/[property]]`, return that property only.
            if property and property in valid_properties.keys():
                job = job[valid_properties[property]]
            self.send_response(job, indent=2)
            self.finish()
            return
        except Exception as e:
            self.send_response(f'''Error querying job records: {e}''', http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return

    def delete(self, job_id=None):
        try:
            assert valid_job_id(job_id)
        except Exception as e:
            self.send_response("Invalid job ID", http_status_code=global_vars.HTTP_BAD_REQUEST, return_json=False)
            self.finish()
            return
        response = ''
        try:
            ## Mark job deleted in `job` table
            db.mark_job_deleted(job_id)
        except Exception as e:
            log.error(f'''Error marking job deleted: {e}''')
            response += 'Error marking job deleted. '
        try:
            ## Delete Kubernetes objects associated with the job
            kubejob.delete_job(job_id=job_id)
        except Exception as e:
            log.error(f'''Error deleting Kubernetes Job objects: {e}''')
            response += 'Error deleting Kubernetes objects. '
        if response:
            self.send_response(response, http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return
        log.debug(f'''Job "{job_id}" deleted successfully.''')
        self.send_response()
        self.finish()
        return


class ResultFileHandler(BaseHandler):
    def get(self, job_id=None, result_id=None):
        try:
            # If a job_id is provided but it is invalid, then the request is malformed:
            if not valid_job_id(job_id):
                self.send_response('Invalid job ID.', http_status_code=global_vars.HTTP_BAD_REQUEST, indent=2)
                self.finish()
                return
            # If a result_id is not provided, then the request is malformed:
            if not result_id:
                self.send_response('Invalid result ID.', http_status_code=global_vars.HTTP_BAD_REQUEST, indent=2)
                self.finish()
                return
            try:
                result_idx = int(result_id)
                job_files = kubejob.list_job_output_files(job_id)
                file_path = job_files[result_idx]
            except:
                self.send_response('Result file not found.', http_status_code=global_vars.HTTP_NOT_FOUND, return_json=False)
                self.finish()
                return
            if not os.path.isfile(file_path):
                self.send_response('Result file not found.', http_status_code=global_vars.HTTP_NOT_FOUND, return_json=False)
                self.finish()
                return
            # TODO: Consider applying "application/octet-stream" universally given the error rate with the guess_type() function
            content_type, _ = guess_type(file_path)
            if not content_type:
                content_type = "application/octet-stream"
            self.add_header('Content-Type', content_type)
            with open(file_path, 'rb') as source_file:
                self.send_response(source_file.read(), return_json=False)
                self.finish()
                return
        except Exception as e:
            response = str(e).strip()
            log.error(response)
            self.send_response(response, http_status_code=global_vars.HTTP_SERVER_ERROR, indent=2)
            self.finish()
            return

class CLEANSubmitJobHandler(BaseHandler):
    def failed_job_response(self, message):
        responseBody = {
            'jobId': 'failed_job_id',
            'url' : APPCONFIG['baseUrl'] + '/jobId/' + 'failed_job_id',
            'status' : 'failed',
            'created_at': 0,
            'message': message
        }
        return responseBody
    
    def verify_captcha(self, captcha_token):
        hcaptcha_secret = config['hcaptcha']['secret']
        try:
            response = requests.request('POST', f'''https://hcaptcha.com/siteverify''', timeout=2,
                data={
                    'secret': hcaptcha_secret,
                    'response': captcha_token,
                }
            )
            try:
                assert response.status_code in [200, 204]
                result = response.json()
                if result['success'] == True:
                    return True
                else:
                    log.error(f'''Invalid CAPTCHA''')
                    return False
            except:
                log.error(f'''Could not verify CAPTCHA''')
                return False
        except Timeout:
            log.warning(f'''Could not verify CAPTCHA''')
            return False
        
    def post(self):
        try:
            data = json.loads(self.request.body)
            user_email = 'dummy@example.com'
            job_config = ""
            
            if len(data) == 0:
                raise Exception('JSON body is empty.')
            if 'user_email' in data:
                user_email = data['user_email']
            if 'captcha_token' in data:
                if not self.verify_captcha(data['captcha_token']):
                    raise Exception('Captcha is invalid.')
            else:
                raise Exception('Captcha is missing.')
            
            sequence_count = 0
            # Convert JSON to FASTA format
            for record in data['input_fasta']:
                # Check for valid amino acid characters
                if not re.match('^[ACDEFGHIKLMNPQRSTVWY]+$', record["sequence"]):
                    raise Exception('Invalid FASTA Protein Sequence.')
                sequence_count += 1
                job_config += ">{}\n{}\n".format(record["header"], record["sequence"])

            if sequence_count > 20:
                raise Exception('CLEAN allows only for a maximum of 20 FASTA Sequences.')

            ## Command that the job container will execute. The `$JOB_OUTPUT_DIR` environment variable is
            ## populated at run time after a job ID and output directory have been provisioned.
            command = f'''cat /tmp/input.fasta'''

            ## environment is a list of environment variable names and values like [{'name': 'env1', 'value': 'val1'}]
            environment = self.getarg('environment', default=[]) # optional

            ## Number of parallel job containers to run. The containers will execute identical code. Coordination is the
            ## responsibility of the job owner.
            ## replicas = self.getarg('replicas', default=1) # optional
            replicas = 1

            ## Valid run_id value follows the Kubernetes label value constraints:
            ##   - must be 63 characters or less (cannot be empty),
            ##   - must begin and end with an alphanumeric character ([a-z0-9A-Z]),
            ##   - could contain dashes (-), underscores (_), dots (.), and alphanumerics between.
            ## See also:
            ##   - https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#runId
            ##   - https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set
            run_id = self.getarg('run_id', default='') # optional
            if run_id and (not isinstance(run_id, str) or run_id != re.sub(r'[^-._a-zA-Z0-9]', "", run_id) or not re.match(r'[a-zA-Z0-9]', run_id)):
                raise Exception('Invalid run_id. Must be 63 characters or less and begin with alphanumeric character and contain only dashes (-), underscores (_), dots (.), and alphanumerics between.')
            user_id = 'DummyID'
        except Exception as e:
            self.send_response(
                self.failed_job_response(str(e.args[0])), http_status_code=global_vars.HTTP_BAD_REQUEST, return_json=False)
            self.finish()
            return

        response = kubejob.create_job(
            command=command,
            run_id=run_id,
            owner_id=user_id,
            replicas=replicas,
            environment=environment,
            job_config=job_config,
        )
        log.debug(response)
        if response['status'] != global_vars.STATUS_OK:
            self.send_response(response['message'], http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return
        try:
            timeout = 30
            while timeout > 0:
                results = kubejob.list_jobs(
                    job_id=response['job_id'],
                )
                if results['jobs']:
                    job = construct_job_object(results['jobs'][0])
                    try:
                        user_agent = self.request.headers["User-Agent"]
                    except:
                        user_agent = ''
                    queue_position = 0
                    try:
                        ## TODO: Replace with specialized query that uses the SQL count() command
                        job_list = []
                        for phase in ['pending', 'queued', 'executing']:
                            job_list.extend(db.select_job_records(phase=phase, fields=['id']))
                        log.debug(job_list)
                        queue_position = len(job_list)
                    except Exception as e:
                        log.error(f'''Error querying job queue position: {e}''')
                    try:
                        command = job['parameters']['command']
                        if isinstance(command, list):
                            command = ' '.join(command)
                        ## TODO: Replace the JSON dump of the full job info with proper table records.
                        job_info = {
                            'job_id': job['jobId'],
                            'run_id': job['runId'],
                            'user_id': job['ownerId'],
                            'command': command,
                            'type': 'cutout',
                            'phase': job['phase'],
                            'time_created': job['creationTime'] or 0,
                            'time_start': job['startTime'] or 0,
                            'time_end': job['endTime'] or 0,
                            'user_agent': user_agent,
                            'email': user_email,
                            'job_info': json.dumps(job, default = self.json_converter),
                            'queue_position': queue_position,
                            }
                        db.insert_job_record(job_info)
                        ##
                        ## TODO: This is a hack for testing locally, because the monitor sidecar container
                        ##       would be calling back to the API server with a job start and end report.
                        ##
                        if not config['uws']['job']['monitorEnabled']:
                            log.debug(f'''Updating started job "{job_info['job_id']}"...''')
                            db.update_job(
                                job_id=job_info['job_id'],
                                phase='executing',
                                start_time=datetime.utcnow(),
                            )
                    except Exception as e:
                        err_msg = f'''Error recording job in database: {e}'''
                        log.error(err_msg)
                        self.send_response(err_msg, http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
                        self.finish()
                        return
                    responseBody = {
                        'jobId': job['jobId'],
                        'url' : APPCONFIG['baseUrl'] + '/jobId/' + job['jobId'],
                        'status' : 'executing',
                        'created_at': job['creationTime'] or 0
                    }
                    self.send_response(responseBody, indent=2)
                    self.finish()
                    return
                else:
                    timeout -= 1
                    time.sleep(0.300)
            self.send_response("Job creation timed out.", http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return
        except Exception as e:
            self.send_response(str(e), http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return
        

class CLEANStatusJobHandler(BaseHandler):
    def post(self):
        job_id = self.getarg('jobId', default='')

        # See https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#resourceuri
        ## The valid properties are the keys of the returned job data structure
        valid_properties = {
            'phase': 'phase',
            'user_id': 'results',
            'command': 'command',
            'run_id': 'run_id',
            'email': 'email',
            'type': 'type',
            'job_info': 'job_info',
        }
        fields = ['job_id', 'phase', 'time_created']
        response = {}
        # If no job_id is included in the request URL, return a list of jobs. See:
        # UWS Schema: https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#UWSSchema
        if not job_id:
            phase = self.getarg('phase', default='') # optional
            if not phase or phase in global_vars.VALID_JOB_STATUSES:
                # results = kubejob.list_jobs()
                try:
                    ## Query for all jobs belonging to the authenticated user
                    # update_job_status(user_id=self._token_decoded["user_id"])
                    # job_list = db.select_job_records(user_id=self._token_decoded["user_id"], phase=phase)
                    job_list = db.select_job_records(user_id='DummyID', phase=phase, fields=fields)
                    responseList = []
                    for job in job_list:
                        job['url'] = APPCONFIG['baseUrl'] + '/jobId/' + job['job_id']
                        iso_8601_str = job['time_created'].replace(tzinfo=utc_timezone).isoformat()
                        responseObject = {'jobId':job['job_id'], 'url': job['url'], 'status': job['phase'], 'created_at': iso_8601_str}
                        responseList.append(responseObject)
                    self.send_response(responseList, indent=2)
                    self.finish()
                    return
                except Exception as e:
                    self.send_response(f'''Error querying job records: {e}''', http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
                    self.finish()
                    return
            else:
                response = 'Valid job categories are: {}'.format(global_vars.VALID_JOB_STATUSES)
                self.send_response(response, http_status_code=global_vars.HTTP_BAD_REQUEST, return_json=False)
                self.finish()
                return
        # If a job_id is provided but it is invalid, then the request is malformed:
        if not valid_job_id(job_id):
            self.send_response('Invalid job ID.', http_status_code=global_vars.HTTP_BAD_REQUEST, indent=2)
            self.finish()
            return
        # If a property is provided but it is invalid, then the request is malformed:
        elif isinstance(property, str) and property not in valid_properties:
            self.send_response(f'Invalid job property requested. Supported properties are {", ".join([key for key in valid_properties.keys()])}', http_status_code=global_vars.HTTP_BAD_REQUEST, indent=2)
            self.finish()
            return
        try:
            ## Query the specified job by ID
            # update_job_status(job_id=job_id)
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
            job['url'] = APPCONFIG['baseUrl'] + '/jobId/' + job['job_id']
            iso_8601_str = job['time_created'].replace(tzinfo=utc_timezone).isoformat()
            responseObject = {'jobId':job['job_id'], 'url': job['url'], 'status': job['phase'], 'created_at': iso_8601_str}
            self.send_response(responseObject, indent=2)
            self.finish()
            return
        except Exception as e:
            self.send_response(f'''Error querying job records: {e}''', http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return    
            


class CLEANResultJobHandler(BaseHandler):
     def post(self):
        job_id = self.getarg('jobId', default='')

        # See https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#resourceuri
        ## The valid properties are the keys of the returned job data structure
        valid_properties = {
            'phase': 'phase',
            'user_id': 'results',
            'command': 'command',
            'run_id': 'run_id',
            'email': 'email',
            'type': 'type',
            'job_info': 'job_info',
        }
        fields = ['job_id', 'phase', 'time_created']
        response = {}
        # If no job_id is included in the request URL, return a list of jobs. See:
        # UWS Schema: https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#UWSSchema
        if not job_id:
            self.send_response('Empty job ID.', http_status_code=global_vars.HTTP_BAD_REQUEST, indent=2)
            self.finish()
            return
        # If a job_id is provided but it is invalid, then the request is malformed:
        if not valid_job_id(job_id):
            self.send_response('Invalid job ID.', http_status_code=global_vars.HTTP_BAD_REQUEST, indent=2)
            self.finish()
            return
        # If a property is provided but it is invalid, then the request is malformed:
        elif isinstance(property, str) and property not in valid_properties:
            self.send_response(f'Invalid job property requested. Supported properties are {", ".join([key for key in valid_properties.keys()])}', http_status_code=global_vars.HTTP_BAD_REQUEST, indent=2)
            self.finish()
            return
        try:
            ## Query the specified job by ID
            # update_job_status(job_id=job_id)
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
            job['url'] = APPCONFIG['baseUrl'] + '/jobId/' + job['job_id']
            output_dir = '/app/results/inputs/'
            fileName = job['job_id'] + '_maxsep.csv'
            iso_8601_str = job['time_created'].replace(tzinfo=utc_timezone).isoformat()
            responseObject = {'jobId':job['job_id'], 'url': job['url'], 'status': job['phase'], 'created_at': iso_8601_str}
            input_str = ''
            with open(output_dir + fileName, 'r') as f:
                input_str = f.read().strip()
            
            lines = input_str.split('\n')
            # create a list to store the results
            results = []

            # process each line
            for line in lines:
                # split the line into the sequence header and the EC numbers/scores
                seq_header, ec_scores_str = line.split(',', 1)
                
                # create a dictionary to store the sequence header and the EC numbers/scores
                result = {
                    "sequence": seq_header,
                    "result": []
                }
                
                # split the EC numbers/scores string into individual EC number/score pairs
                ec_scores_pairs = ec_scores_str.split(',')
                
                # process each EC number/score pair
                for ec_score_pair in ec_scores_pairs:
                    # split the EC number/score pair into the EC number and the score
                    ec_number, score = ec_score_pair.split('/')
                    
                    # create a dictionary to store the EC number and the score
                    ec_score_dict = {
                        "ecNumber": ec_number,
                        "score": score
                    }
                    
                    # add the EC number/score dictionary to the result list
                    result["result"].append(ec_score_dict)
                
                # add the result dictionary to the results list
                results.append(result)

            # convert the results list to JSON and print it
            # json_str = json.dumps(results, indent=4)

            responseObject['results'] = results
            self.send_response(responseObject, indent=2)
            self.finish()
            return
        except Exception as e:
            self.send_response(f'''Error querying job records: {e}''', http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return    


class CLEANAddMailingListHandler(BaseHandler):
    def post(self):
        try:
            email = self.getarg('email', default='')
            if not email or not self.is_valid_email(email=email):
                raise Exception('Invalid Email Address Provided.')
            db.insert_email_to_mailing_list(email=email)
            self.send_response({'status': 'true', 'message': 'Added Email to the Mailing List.'})
            self.finish()
            return
        except Exception as e:
            insert_fail_json = {
                'status':'false',
                'message': str(e.args[0])
            }
            self.send_response(data=insert_fail_json, http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return
        
class CLEANRemoveMailingListHandler(BaseHandler):
    def post(self):
        try:
            email = self.getarg('email', default='')
            if not email or not self.is_valid_email(email=email):
                raise Exception('Invalid Email Address Provided.')
            db.remove_email_from_mailing_list(email=email)
            self.send_response({'status': 'true', 'message': 'Removed Email from the Mailing List.'})
            self.finish()
            return
        except Exception as e:
            insert_fail_json = {
                'status':'false',
                'message': str(e.args[0])
            }
            self.send_response(data=insert_fail_json, http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return

def make_app(app_base_path='/', api_base_path='api', debug=False):
    ## Configure app base path
    app_base_path = f'''/{app_base_path.strip('/')}'''
    if app_base_path == '/':
        app_base_path = ''
    ## Configure API base path
    api_base_path = api_base_path.strip('/')
    settings = {
        "debug": debug,
        "login_url": r"{}/account/login".format(app_base_path),
        "cookie_secret": bcrypt.gensalt().decode('utf-8'),
        "static_path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
    }
    settings['public_path'] = os.path.join(settings['static_path'], "app/public")
    return tornado.web.Application(
        [
            (r"{}/app/public/(.*)".format(app_base_path), tornado.web.StaticFileHandler, {'path': settings['public_path']}),
            (r"{}/{}/uws/job/result/(.*)/(.*)".format(app_base_path, api_base_path), ResultFileHandler),
            (r"{}/{}/uws/job/(.*)/(.*)".format(app_base_path, api_base_path), JobHandler),
            (r"{}/{}/uws/job/(.*)".format(app_base_path, api_base_path), JobHandler),
            (r"{}/{}/uws/job".format(app_base_path, api_base_path), JobHandler),
            (r"{}/{}/uws/report/start/(.*)".format(app_base_path, api_base_path), JobReportStartHandler),
            (r"{}/{}/uws/report/end/(.*)".format(app_base_path, api_base_path), JobReportCompleteHandler),
            (r"{}/{}/job/submit".format(app_base_path, api_base_path), CLEANSubmitJobHandler),
            (r"{}/{}/job/status".format(app_base_path, api_base_path), CLEANStatusJobHandler),
            (r"{}/{}/job/result".format(app_base_path, api_base_path), CLEANResultJobHandler),
            (r"{}/{}/mailing/add".format(app_base_path, api_base_path), CLEANAddMailingListHandler),
            (r"{}/{}/mailing/delete".format(app_base_path, api_base_path), CLEANRemoveMailingListHandler),
        ],
        **settings
    )


if __name__ == "__main__":
    try:
        # Wait for database to come online if it is still starting
        waiting_for_db = True
        while waiting_for_db:
            try:
                db.open_db_connection()
                waiting_for_db = False
                db.close_db_connection()
            except Exception as e:
                log.error(f'Unable to connect to database. Waiting to try again: {str(e)}')
                time.sleep(5.0)
        ## Create/update database tables
        db.update_db_tables()
    except Exception as e:
        log.error(str(e).strip())
    ## Create TornadoWeb application
    app = make_app(
        app_base_path=config['server']['basePath'],
        api_base_path=config['server']['apiBasePath'],
        debug=config['server']['debug']
    )
    app.listen(int(config['server']['port']))
    log.info(f'''API server listening on port {config['server']['port']} at base path "{config['server']['basePath']}"''')
    log.debug(json.dumps(config, indent=2))
    tornado.ioloop.IOLoop.current().start()
