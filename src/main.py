import base64
import os
import uuid

import global_vars
from global_vars import config, log
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

import userinfo
import kubewatcher

# Get global instance of the job handler database interface
db = DbConnector(
    mysql_host=config['db']['host'],
    mysql_user=config['db']['user'],
    mysql_password=config['db']['pass'],
    mysql_database=config['db']['database'],
)

APPCONFIG = {
    # 'auth_token': os.environ['SPT_API_TOKEN'],
    'baseUrl': '/',
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
        # Parse JSON request body
        data = json.loads(self.request.body)

        user = None
        if 'oauth' in config and 'cookieName' in config['oauth'] and config['oauth']['cookieName'] in self.request.cookies:
            user = userinfo.validate_auth_cookie(self.request)
            #log.debug('User: ' + str(user))

        if user:
            user_email = user['email']
        elif 'captcha_token' in data:
            log.warning('401 Unauthorized - falling back to captcha')
            user_email = data['user_email'] if 'user_email' in data else ''
            # Fallback attempt to hcaptcha without _oauth2_proxy cookie
            if userinfo.verify_captcha(data['captcha_token']):
                pass
            else:
                # No auth token, no captcha => no access
                self.send_response(data='401: Unauthorized',
                                   http_status_code=401,
                                   return_json=False)
                self.finish()
                return
        else:
            # No auth token, no captcha => no access
            self.send_response(data='401: Unauthorized',
                               http_status_code=401,
                               return_json=False)
            self.finish()
            return


        try:
            echo = self.getarg('echo') # required

            job_config = {
                'echo': echo,
            }

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

        ## Command that the job container will execute. The `$JOB_OUTPUT_DIR` environment variable is
        ## populated at run time after a job ID and output directory have been provisioned.
        encoded_data = base64.b64encode(job_config.encode('utf-8')).decode('utf-8')
        mount_path = '/app/data/inputs'
        command = f'''echo {encoded_data} | base64 -d > {mount_path}/{run_id}.fasta && ((python CLEAN_infer_fasta.py --fasta_data {run_id} >> {job_output_dir}/log) || (touch {job_output_dir}/error && false))'''
        log.debug(f"Job command: {command}")

        ## Options:
        response = kubejob.create_job(
            image_name='moleculemaker/clean-image-amd64',
            command=command,
            run_id=run_id,
            owner_id=user_id,
            replicas=replicas,
            environment=environment,
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
                            'type': 'clean',
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
            'type': 'type',
            'job_info': 'job_info',
        }

        # Check for user, only list email if user logged in
        user = None
        if 'oauth' in config and 'cookieName' in config['oauth'] and config['oauth']['cookieName'] in self.request.cookies:
            user = userinfo.validate_auth_cookie(self.request)
            #log.debug('User: ' + str(user))

        if user is not None:
            valid_properties['email'] = 'email'

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
        # Only allow destructive operation for logged-in user
        user = None
        if 'oauth' in config and 'cookieName' in config['oauth'] and config['oauth']['cookieName'] in self.request.cookies:
            user = userinfo.validate_auth_cookie(self.request)
            #log.debug('User: ' + str(user))

        if user is None:
            log.error('401 Unauthorized')
            self.send_response('401: Unauthorized', http_status_code=401, return_json=False)
            self.finish()
            return

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

class MOLLIJobHandler(BaseHandler):
    def post(self):


        user = None
        if 'oauth' in config and 'cookieName' in config['oauth'] and config['oauth'][
            'cookieName'] in self.request.cookies:
            user = userinfo.validate_auth_cookie(self.request)
            #log.debug('User: ' + str(user))

        # Parse JSON request body
        try:
            #data = json.loads(self.request.body)
            data = self.request.files
        except:
            # Invalid DNA Sequence, return 400
            self.send_response(data='400: Bad Request - JSON body expected',
                               http_status_code=400,
                               return_json=False)
            self.finish()
            return


        if len(data) == 0:
            # Invalid DNA Sequence, return 400
            self.send_response(data='400: Bad Request - JSON body expected',
                               http_status_code=400,
                               return_json=False)
            self.finish()
            return

        user_email = ''
        if user:
            user_email = user['email']
        elif 'captcha_token' in data:
            log.warning('401 Unauthorized - falling back to captcha')
            user_email = data['user_email'] if 'user_email' in data else ''
            # Fallback attempt to hcaptcha without _oauth2_proxy cookie
            if userinfo.verify_captcha(data['captcha_token']):
                pass
            else:
                # No auth token, no captcha => no access
                self.send_response(data='401: Unauthorized',
                                   http_status_code=401,
                                   return_json=False)
                self.finish()
                return
        else:
            # No auth token, no captcha => no access
            self.send_response(data='401: Unauthorized',
                               http_status_code=401,
                               return_json=False)
            self.finish()
            return

        if not data or 'cores' not in data or 'subs' not in data:
            # No auth token, no captcha => no access
            self.send_response(data='400: Bad Request - both "cores" and "subs" are required',
                               http_status_code=400,
                               return_json=False)
            self.finish()
            return

        cores_file_data = data['cores']
        subs_file_data = data['subs']

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
            # Invalid FASTA Sequence, return 400
            self.send_response(data='400: Bad Request - Invalid run_id. Must be 63 characters or less and begin with alphanumeric character and contain only dashes (-), underscores (_), dots (.), and alphanumerics between.',
                               http_status_code=400,
                               return_json=False)
            self.finish()
            return

        user_id = 'DummyID'

        # Build up path to output dir
        # FIXME: feature envy?
        job_id = kubejob.generate_uuid()
        if not run_id:
            run_id = job_id

        # Write user input files to the shared folder
        input_dir_in_apiserver = '/uws/job/input'
        for name in ['cores', 'subs']:
            # Read uploaded file
            fileinfo = self.request.files[name][0]
            #log.debug(f"File info: {fileinfo}")

            # Write file to dest_folder
            filepath = f'{input_dir_in_apiserver}/{job_id}.{name}.cdxml'
            log.debug(f'[molli:{job_id}]  Writing {name} file to: {filepath}')
            fh = open(filepath, 'wb')
            fh.write(fileinfo['body'])
            fh.close()

        # Tell the job to use these files when it runs
        input_dir_in_container = '/app/data/inputs'
        environment.append({ 'name': 'CORES_INPUT_FILE', 'value': f'{input_dir_in_container}/{job_id}.cores.cdxml'})
        environment.append({ 'name': 'SUBS_INPUT_FILE', 'value': f'{input_dir_in_container}/{job_id}.subs.cdxml'})


        response = kubejob.create_job(
            image_name='ghcr.io/moleculemaker/molli:ncsa-k8s-job',
            command="/molli/entrypoint.sh",
            job_id=job_id,
            run_id=run_id,
            owner_id=user_id,
            replicas=replicas,
            environment=environment
        )
        log.debug(response)
        if response['status'] != global_vars.STATUS_OK:
            self.send_response(response['message'], http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return
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
                        'type': 'molli',
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
                    'url' : '/jobId/' + job['jobId'],
                    'status' : 'executing',
                    'created_at': job['creationTime'] or 0
                }
                self.send_response(responseBody, indent=2, return_json=True)
                self.finish()
                return
            else:
                timeout -= 1
                time.sleep(0.300)
        self.send_response("Job creation timed out.", http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
        self.finish()
        return

    # TODO: Currently unused, not yet tested
    def put(self, job_id=None, phase=None):
        if job_id is None or phase is None:
            # No auth token, no captcha => no access
            self.send_response(data='400: Bad Request - both "job_id" and "phase" are required',
                               http_status_code=400,
                               return_json=False)
            self.finish()
            return

        if not phase in global_vars.VALID_JOB_STATUSES:
            # No auth token, no captcha => no access
            self.send_response(data=f'400: Bad Request - phase={phase} is invalid. Try one of {global_vars.VALID_JOB_STATUSES}',
                               http_status_code=400,
                               return_json=False)
            self.finish()
            return


        fields = ['job_id', 'phase', 'time_created']
        response = {}
        # If no job_id is included in the request URL, return a list of jobs. See:
        # UWS Schema: https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#UWSSchema

        try:
            ## Query for all jobs belonging to the authenticated user
            # update_job_status(user_id=self._token_decoded["user_id"])
            # job_list = db.select_job_records(user_id=self._token_decoded["user_id"], phase=phase)
            log.debug(f'Updating: {job_id} -> {phase}')
            db.update_job(job_id=job_id, phase=phase)

        except Exception as e:
            log.error(f'Error while updating job phase - {str(e)}')



    def get(self, job_id=None):
        #job_id = self.getarg('jobId', default='')

        # See https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#resourceuri
        ## The valid properties are the keys of the returned job data structure
        valid_properties = {
            'phase': 'phase',
            'user_id': 'results',
            'command': 'command',
            'run_id': 'run_id',
            'type': 'type',
            'job_info': 'job_info',
        }

        # Check for user, only list email if user logged in
        user = None
        if 'oauth' in config and 'cookieName' in config['oauth'] and config['oauth'][
            'cookieName'] in self.request.cookies:
            user = userinfo.validate_auth_cookie(self.request)
            #log.debug('User: ' + str(user))

        if user is not None:
            valid_properties['email'] = 'email'

        fields = ['job_id', 'phase', 'time_created']
        response = {}
        # If no job_id is included in the request URL, return a list of jobs. See:
        # UWS Schema: https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#UWSSchema
        if not job_id:

            # No auth token, no captcha => no access
            self.send_response(data=f'400: Bad Request - phase={phase} is invalid. Try one of {global_vars.VALID_JOB_STATUSES}',
                               http_status_code=400,
                               return_json=False)
            self.finish()
            return

        # results = kubejob.list_jobs()
        try:
            ## Query for all jobs belonging to the authenticated user
            # update_job_status(user_id=self._token_decoded["user_id"])
            # job_list = db.select_job_records(user_id=self._token_decoded["user_id"], phase=phase)
            job_list = db.select_job_records(job_id=job_id, fields=fields)
            responseList = []
            for job in job_list:
                job['url'] = '/jobId/' + job['job_id']
                iso_8601_str = job['time_created']
                responseObject = {'jobId': job['job_id'], 'url': job['url'], 'status': job['phase'],
                                  'created_at': iso_8601_str}
                self.send_response(responseObject, indent=2, return_json=True)
            self.finish()
            return
        except Exception as e:
            self.send_response(f'''Error querying job records: {e}''',
                               http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return


class MOLLIResultFileHandler(BaseHandler):

    def get(self, job_id=None, file_name=None):
        #job_id = self.getarg('jobId', default='')
        log.info(f'Fetching results for: {job_id}')

        # See https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#resourceuri
        ## The valid properties are the keys of the returned job data structure
        valid_properties = {
            'phase': 'phase',
            'user_id': 'results',
            'command': 'command',
            'run_id': 'run_id',
            'type': 'type',
            'job_info': 'job_info',
        }

        # Check for user, only list email if user logged in
        user = None
        if 'oauth' in config and 'cookieName' in config['oauth'] and config['oauth'][
            'cookieName'] in self.request.cookies:
            user = userinfo.validate_auth_cookie(self.request)
            #log.debug('User: ' + str(user))

        if user is not None:
            valid_properties['email'] = 'email'

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
            self.send_response(
                f'Invalid job property requested. Supported properties are {", ".join([key for key in valid_properties.keys()])}',
                http_status_code=global_vars.HTTP_BAD_REQUEST, indent=2)
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


            # Read output file from disk
            output_dir = '/uws/job/uws/jobs/'

            if file_name:
                resultJsonFullPath = os.path.join(output_dir, job['job_id'], 'out', file_name)
                log.debug(f'Returning MOLLI result file: {resultJsonFullPath}')
                with open(resultJsonFullPath, 'r') as f:
                    file_contents = f.read().strip()

                self.finish()
                return
            else:
                gen_struct_file_path = os.path.join(output_dir, job['job_id'], 'out', 'test_combine_new_env_library.json')
                clustering_pca_file_path = os.path.join(output_dir, job['job_id'], 'out', 'new_env_data3_pca')
                clustering_tsne_file_path = os.path.join(output_dir, job['job_id'], 'out', 'new_env_data3_tsne')
                with open(gen_struct_file_path, 'r') as gen, open(clustering_pca_file_path, 'r') as pca, open(clustering_tsne_file_path, 'r') as tsne:
                    response_obj = {
                        'jobId': job['job_id'],
                        'url': '/results/' + job['job_id'],
                        'status': job['phase'],
                        'created_at': job['time_created'].replace(tzinfo=utc_timezone).isoformat(),
                        'results': {
                            'structures': json.loads(gen.read().strip()),
                            'clusteringData': {
                                'pca': json.loads(pca.read().strip()),
                                'tsne': json.loads(tsne.read().strip())
                            }
                        }
                    }
                    self.send_response(response_obj, indent=2, return_json=True)

                self.finish()
                return
        except Exception as e:
            self.send_response(f'''Error querying job records: {e}''', http_status_code=global_vars.HTTP_SERVER_ERROR,
                               return_json=False)
            self.finish()
            return


class CLEANSubmitJobHandler(BaseHandler):
    def failed_job_response(self, message):
        responseBody = {
            'jobId': 'failed_job_id',
            'url' : '/jobId/' + 'failed_job_id',
            'status' : 'failed',
            'created_at': 0,
            'message': message
        }
        return responseBody

    def getProteinSeqFromDNA(self, DNA_sequence):
        # define a codon table as a dictionary
        codon_table = {
            'ATA':'I', 'ATC':'I', 'ATT':'I', 'ATG':'M',
            'ACA':'T', 'ACC':'T', 'ACG':'T', 'ACT':'T',
            'AAC':'N', 'AAT':'N', 'AAA':'K', 'AAG':'K',
            'AGC':'S', 'AGT':'S', 'AGA':'R', 'AGG':'R',                
            'CTA':'L', 'CTC':'L', 'CTG':'L', 'CTT':'L',
            'CCA':'P', 'CCC':'P', 'CCG':'P', 'CCT':'P',
            'CAC':'H', 'CAT':'H', 'CAA':'Q', 'CAG':'Q',
            'CGA':'R', 'CGC':'R', 'CGG':'R', 'CGT':'R',
            'GTA':'V', 'GTC':'V', 'GTG':'V', 'GTT':'V',
            'GCA':'A', 'GCC':'A', 'GCG':'A', 'GCT':'A',
            'GAC':'D', 'GAT':'D', 'GAA':'E', 'GAG':'E',
            'GGA':'G', 'GGC':'G', 'GGG':'G', 'GGT':'G',
            'TCA':'S', 'TCC':'S', 'TCG':'S', 'TCT':'S',
            'TTC':'F', 'TTT':'F', 'TTA':'L', 'TTG':'L',
            'TAC':'Y', 'TAT':'Y', 'TAA':'*', 'TAG':'*',
            'TGC':'C', 'TGT':'C', 'TGA':'*', 'TGG':'W',
        }

        nucleotide_seq = DNA_sequence

        # assert that the length of the nucleotide sequence is a multiple of 3
        if len(nucleotide_seq) % 3 != 0:
            log.error(f'''Length of nucleotide sequence is not a multiple of 3''')
            return ''
        # split the nucleotide sequence into codons
        codons = [nucleotide_seq[i:i+3] for i in range(0, len(nucleotide_seq), 3)]
        # translate each codon into its corresponding amino acid
        amino_acids = [codon_table[codon] for codon in codons]
        # join the resulting amino acid sequence into a string
        amino_acid_seq = "".join(amino_acids)
        # print the resulting amino acid sequence
        return amino_acid_seq
        
    def post(self):
        user = None
        if 'oauth' in config and 'cookieName' in config['oauth'] and config['oauth']['cookieName'] in self.request.cookies:
            user = userinfo.validate_auth_cookie(self.request)
            #log.debug('User: ' + str(user))

        # Parse JSON request body
        data = json.loads(self.request.body)

        if len(data) == 0:
            # Invalid DNA Sequence, return 400
            self.send_response(data='400: Bad Request - JSON body expected',
                               http_status_code=400,
                               return_json=False)
            self.finish()
            return

        if user:
            user_email = user['email']
        elif 'captcha_token' in data:
            log.warning('401 Unauthorized - falling back to captcha')
            user_email = data['user_email'] if 'user_email' in data else ''
            # Fallback attempt to hcaptcha without _oauth2_proxy cookie
            if userinfo.verify_captcha(data['captcha_token']):
                pass
            else:
                # No auth token, no captcha => no access
                self.send_response(data='401: Unauthorized',
                                   http_status_code=401,
                                   return_json=False)
                self.finish()
                return
        else:
            # No auth token, no captcha => no access
            self.send_response(data='401: Unauthorized',
                               http_status_code=401,
                               return_json=False)
            self.finish()
            return

        job_config = ""

        sequence_count = 0
        # Convert JSON to FASTA format
        for record in data['input_fasta']:
            # Check for valid amino acid characters
            if re.match('^[ACDEFGHIKLMNPQRSTVWY]+$', record["sequence"]):
                job_config += ">{}\n{}\n".format(record["header"], record["sequence"])
            elif re.match('^[ACGTURYSWKMBDHVN]*$', record["DNA_sequence"]):
                protein_sequence = self.getProteinSeqFromDNA(record["DNA_sequence"])
                if protein_sequence == '':
                    # Invalid DNA Sequence, return 400
                    self.send_response(data='400: Bad Request - Invalid DNA Sequence',
                                       http_status_code=400,
                                       return_json=False)
                    self.finish()
                    return
                else:
                    job_config += ">{}\n{}\n".format(record["header"], protein_sequence)
            else:
                # Invalid FASTA Sequence, return 400
                self.send_response(data='400: Bad Request - Invalid FASTA Protein Sequence',
                                   http_status_code=400,
                                   return_json=False)
                self.finish()
                return

            sequence_count += 1

        if sequence_count > 20:
            # Invalid FASTA Sequence, return 400
            self.send_response(data='400: Bad Request - CLEAN allows only for a maximum of 20 FASTA Sequences.',
                               http_status_code=400,
                               return_json=False)
            self.finish()
            return

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
            # Invalid FASTA Sequence, return 400
            self.send_response(data='400: Bad Request - Invalid run_id. Must be 63 characters or less and begin with alphanumeric character and contain only dashes (-), underscores (_), dots (.), and alphanumerics between.',
                               http_status_code=400,
                               return_json=False)
            self.finish()
            return

        user_id = 'DummyID'

        # Build up path to output dir
        # FIXME: feature envy?
        job_id = kubejob.generate_uuid()
        if not run_id:
            run_id = job_id
        job_root_dir = kubejob.get_job_root_dir_from_id(job_id)
        job_output_dir = os.path.join(job_root_dir, 'out')

        ## Command that the job container will execute. The `$JOB_OUTPUT_DIR` environment variable is
        ## populated at run time after a job ID and output directory have been provisioned.
        encoded_data = base64.b64encode(job_config.encode('utf-8')).decode('utf-8')
        mount_path = '/app/data/inputs'
        command = f'''echo {encoded_data} | base64 -d > {mount_path}/{run_id}.fasta && ((python CLEAN_infer_fasta.py --fasta_data {run_id} >> {job_output_dir}/log) || (touch {job_output_dir}/error && false))'''

        response = kubejob.create_job(
            image_name='moleculemaker/clean-image-amd64',
            command=command,
            job_id=job_id,
            run_id=run_id,
            owner_id=user_id,
            replicas=replicas,
            environment=environment
        )
        log.debug(response)
        if response['status'] != global_vars.STATUS_OK:
            self.send_response(response['message'], http_status_code=global_vars.HTTP_SERVER_ERROR, return_json=False)
            self.finish()
            return
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
                        'type': 'clean',
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
                    'url' : '/jobId/' + job['jobId'],
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
            'type': 'type',
            'job_info': 'job_info',
        }

        # Check for user, only list email if user logged in
        user = None
        if 'oauth' in config and 'cookieName' in config['oauth'] and config['oauth']['cookieName'] in self.request.cookies:
            user = userinfo.validate_auth_cookie(self.request)
            #log.debug('User: ' + str(user))

        if user is not None:
            valid_properties['email'] = 'email'

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
                        job['url'] = '/jobId/' + job['job_id']
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
            job['url'] = '/jobId/' + job['job_id']
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
            'type': 'type',
            'job_info': 'job_info',
        }

        # Check for user, only list email if user logged in
        user = None
        if 'oauth' in config and 'cookieName' in config['oauth'] and config['oauth']['cookieName'] in self.request.cookies:
            user = userinfo.validate_auth_cookie(self.request)
            #log.debug('User: ' + str(user))

        if user is not None:
            valid_properties['email'] = 'email'

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
            job['url'] = '/jobId/' + job['job_id']
            output_dir = f'/uws/job/output/'
            fileName = job['job_id'] + '_maxsep.csv'
            iso_8601_str = job['time_created'].replace(tzinfo=utc_timezone).isoformat()
            responseObject = {'jobId':job['job_id'], 'url': job['url'], 'status': job['phase'], 'created_at': iso_8601_str}
            fullPath = os.path.join(output_dir, fileName)
            log.debug(f'Returning CLEAN result file: {fullPath}')
            input_str = ''
            with open(fullPath, 'r') as f:
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
                'message': str(e)
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
                'message': str(e)
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
            (r"{}/{}/clean/submit".format(app_base_path, api_base_path), CLEANSubmitJobHandler),
            (r"{}/{}/clean/status".format(app_base_path, api_base_path), CLEANStatusJobHandler),
            (r"{}/{}/clean/result".format(app_base_path, api_base_path), CLEANResultJobHandler),
            (r"{}/{}/job/molli/(.*)/results/(.*)".format(app_base_path, api_base_path), MOLLIResultFileHandler),
            (r"{}/{}/job/molli/(.*)/results".format(app_base_path, api_base_path), MOLLIResultFileHandler),
            (r"{}/{}/job/molli/(.*)".format(app_base_path, api_base_path), MOLLIJobHandler),
            (r"{}/{}/job/molli".format(app_base_path, api_base_path), MOLLIJobHandler),
            (r"{}/{}/mailing/add".format(app_base_path, api_base_path), CLEANAddMailingListHandler),
            (r"{}/{}/mailing/delete".format(app_base_path, api_base_path), CLEANRemoveMailingListHandler),

            # LEGACY - remove these after changing frontends to use /{type}/submit
            (r"{}/{}/job/submit".format(app_base_path, api_base_path), CLEANSubmitJobHandler),
            (r"{}/{}/job/status".format(app_base_path, api_base_path), CLEANStatusJobHandler),
            (r"{}/{}/job/result".format(app_base_path, api_base_path), CLEANResultJobHandler),
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

    watcher = kubewatcher.KubeEventWatcher()

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
