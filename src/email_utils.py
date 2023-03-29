import os
import smtplib
import jinja2
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
from global_vars import config


class SingleEmailHeader(object):
    def __init__(self, recipients, email_params, template=''):
        self.recipients = recipients
        self.server = config['email']['server']
        self.from_email = config['email']['fromEmail']
        self.from_name = config['email']['fromName']
        ## Construct email o
        self.msg = MIMEMultipart('alternative')
        self.msg['Subject'] = email_params['subject']
        self.msg['From'] = formataddr((str(Header(self.from_name, 'utf-8')), self.from_email))
        self.msg['To'] = ', '.join(self.recipients)
        ## Render email HTML content
        self.html = self.render(os.path.join(os.path.dirname(__file__), 'templates', template), email_params)
        self.msg.attach(MIMEText(self.html, 'html'))

    def render(self, tpl_path, email_params):
        path, filename = os.path.split(tpl_path)
        return jinja2.Environment(loader=jinja2.FileSystemLoader(path or './')).get_template(filename).render(email_params)

    def sendmail(self):
        smtp_server = smtplib.SMTP(self.server)
        # The TO and CC header fields are populated by the header construction, and any additional recipient addresses are effectively BCC
        smtp_server.sendmail(self.from_email, self.recipients, self.msg.as_string())
        smtp_server.quit()


def send_email(recipients=None, email_subject='', message_body='', template_file=None):
    if not isinstance(recipients, list):
        recipients = [recipients]
    email_params = {
        "subject": email_subject,
        "message": message_body,
    }
    email = SingleEmailHeader(recipients, email_params, template=template_file)
    email.sendmail()


def send_job_complete_email(job={}):
    assert 'job_id' in job
    job_output_url = f'''https://{config['server']['hostName']}{os.path.join('/', config['server']['basePath'], '/files/jobs', job['job_id'], 'out')}'''
    email_subject = f'''SPT-3G job finished: {job['job_id']}'''
    message_body = f'''
    <p>
        <table>
            <tr><td>Job ID:</td><td><code>{job['job_id']}</code></td></tr>
            <tr><td>Run ID:</td><td><code>{job['run_id']}</code></td></tr>
            <tr><td>Status:</td><td><code>{job['phase']}</code></td></tr>
            <tr><td>Output files:</td><td><a href="{job_output_url}"><code>{job_output_url}</code></a></td></tr>
            <tr><td>Quick Download:</td><td><code>wget -r -nH -np --content-disposition --reject "index.html*" {job_output_url}</code></td></tr>
        </table>
    </p>
    <p>
        <b>Note:</b> You have approximately 48 hours to download your job
        files before they are automatically deleted.
    </p>
    '''
    ## Send the email notification
    send_email(recipients=job['email'], email_subject=email_subject,
               message_body=message_body, template_file='email_base.tpl.html')
