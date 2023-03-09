import mysql.connector
from global_vars import log, VALID_JOB_STATUSES
import os
from datetime import datetime


class DbConnector:
    def __init__(self, mysql_host, mysql_user, mysql_password, mysql_database):
        self.host = mysql_host
        self.user = mysql_user
        self.password = mysql_password
        self.database = mysql_database
        self.cur = None
        self.cnx = None
        self.db_schema_version = 1

    def open_db_connection(self):
        if self.cnx is None or self.cur is None:
            # Open database connection
            self.cnx = mysql.connector.connect(
                host=self.host,
                user=self.user,
                password=self.password,
                database=self.database,
            )
            # Get database cursor object
            self.cur = self.cnx.cursor()

    def close_db_connection(self):
        if self.cnx != None and self.cur != None:
            try:
                # Commit changes to database and close connection
                self.cnx.commit()
                self.cur.close()
                self.cnx.close()
                self.cur = None
                self.cnx = None
            except Exception as e:
                error = str(e).strip()
                self.cur = None
                self.cnx = None
                return False, error

    def parse_sql_commands(self, sql_file):
        msg = ''
        commands = []
        try:
            with open(sql_file) as f:
                dbUpdateSql = f.read()
            #Individual SQL commands must be separated by the custom delimiter '#---'
            for sql_cmd in dbUpdateSql.split('#---'):
                if len(sql_cmd) > 0 and not sql_cmd.isspace():
                    commands.append(sql_cmd)
        except Exception as e:
            msg = str(e).strip()
            log.error(msg)
        return commands

    def update_db_tables(self):
        self.open_db_connection()
        try:
            current_schema_version = 0
            try:
                # Get currently applied database schema version if tables have already been created
                self.cur.execute(
                    "SELECT `schema_version` FROM `meta` LIMIT 1"
                )
                for (schema_version,) in self.cur:
                    current_schema_version = schema_version
                log.info("schema_version taken from meta table")
            except:
                log.info("meta table not found")
            log.info('current schema version: {}'.format(current_schema_version))
            # Update the database schema if the versions do not match
            if current_schema_version < self.db_schema_version:
                # Sequentially apply each DB update until the schema is fully updated
                for db_update_idx in range(current_schema_version+1, self.db_schema_version+1, 1):
                    sql_file = os.path.join(os.path.dirname(__file__), "db_schema_update", "db_schema_update.{}.sql".format(db_update_idx))
                    commands = self.parse_sql_commands(sql_file)
                    for cmd in commands:
                        log.info('Applying SQL command from "{}":'.format(sql_file))
                        log.info(cmd)
                        # Apply incremental update
                        self.cur.execute(cmd)
                    # Update `meta` table to match
                    log.info("Updating `meta` table...")
                    try:
                        self.cur.execute(
                            "INSERT INTO `meta` (`schema_version`) VALUES ({})".format(db_update_idx)
                        )
                    except:
                        self.cur.execute(
                            "UPDATE `meta` SET `schema_version`={}".format(db_update_idx)
                        )
                    self.cur.execute(
                        "SELECT `schema_version` FROM `meta` LIMIT 1"
                    )
                    for (schema_version,) in self.cur:
                        log.info('Current schema version: {}'.format(schema_version))
        except Exception as e:
            log.error(str(e).strip())
        self.close_db_connection()

    def get_denylist(self):
        self.open_db_connection()
        try:
            denylist = []
            self.cur.execute(
                "SELECT `user_id`, `time_added` FROM `user_denylist`"
            )
            for (user_id, time_added) in self.cur:
                denylist.append({
                    'user_id': user_id,
                    'time_added': time_added,
                })
        except Exception as e:
            log.error(str(e).strip())
        self.close_db_connection()
        return denylist

    def update_denylist(self, user_id, action):
        self.open_db_connection()
        try:
            assert action in ['add', 'remove']
            in_denylist = False
            self.cur.execute(
                ("SELECT `user_id` FROM `user_denylist` WHERE `user_id` = %s LIMIT 1"),
                (user_id,)
            )
            for (user_id,) in self.cur:
                in_denylist = True
            if in_denylist and action == 'remove':
                log.debug(f'''Deleting user_id "{user_id}" from `user_denylist`...''')
                self.cur.execute(
                    ("DELETE FROM `user_denylist` WHERE `user_id`=%s"),
                    (user_id,)
                )
            elif not in_denylist and action == 'add':
                log.debug(f'''Inserting user_id "{user_id}" into `user_denylist`...''')
                self.cur.execute(
                    ("INSERT INTO `user_denylist` (`user_id`, `time_added`) VALUES (%s, %s)"),
                    (user_id, datetime.utcnow())
                )
            self.close_db_connection()
        except Exception as e:
            log.error(e)
            self.close_db_connection()
            raise

    def insert_job_record(self, job_info):
        self.open_db_connection()
        try:
            placeholders = ', '.join(['%s'] * len(job_info))
            columns = ', '.join(job_info.keys())
            self.cur.execute(
                (f"INSERT INTO `job` ({columns}) VALUES ({placeholders})"),
                tuple(job_info.values())
            )
            self.close_db_connection()
        except Exception as e:
            log.error(e)
            self.close_db_connection()
            raise

    def select_job_records(self, job_id: str = '', user_id: str = '', phase: str = '', deleted: bool = False, hasEmailAddress: bool = False, fields: list = []) -> list:
        self.open_db_connection()
        try:
            assert phase in VALID_JOB_STATUSES or not phase
            criteria = '`id`!=0' # Dummy criterion in case no others are added
            ## To select regardless of deleted status, specify deleted = None
            if isinstance(deleted, bool):
                deleted_val = 1 if deleted == True else 0
                criteria += f' AND `deleted`={deleted_val}'
            vals = ()
            if hasEmailAddress:
                criteria += ' AND `email`!=""'
            if job_id:
                criteria += ' AND `job_id`=%s'
                vals += (job_id,)
            elif user_id:
                criteria += ' AND `user_id`=%s'
                vals += (user_id,)
            if phase:
                criteria += ' AND `phase`=%s'
                vals += (phase,)
            if isinstance(fields, list) and len(fields) > 0:
                fields = f'''`{'`, `'.join(fields)}`'''
            else:
                fields = '*'
            sql = f'''
                SELECT {fields} FROM `job`
                WHERE {criteria}
                ORDER BY `time_created` DESC
            '''
            log.debug(f'sql: {sql}')
            log.debug(f'vals: {vals}')
            job_info_list = []
            self.cur.execute(sql, vals)
            col_names = [desc[0] for desc in self.cur.description]
            for record in self.cur.fetchall():
                job_info = {}
                idx = 0
                for col_val in record:
                    job_info[col_names[idx]] = col_val
                    idx += 1
                job_info_list.append(job_info)
            self.close_db_connection()
            return job_info_list
        except Exception as e:
            log.error(e)
            self.close_db_connection()
            raise

    def mark_job_deleted(self, job_id: str = '') -> None:
        self.open_db_connection()
        try:
            assert job_id
            self.cur.execute(
                (f'''UPDATE `job` SET `deleted`=%s WHERE `job_id`=%s'''),
                (True, job_id)
            )
            self.close_db_connection()
        except Exception as e:
            log.error(f'''Error updating job record: {e}''')
            self.close_db_connection()
            raise

    def update_job(self, job_id: str = '', phase: str = '', start_time: str = '', end_time: str = '', email: str = None, queue_position: int = None) -> None:
        self.open_db_connection()
        try:
            assert job_id
            assert phase or start_time or end_time or isinstance(email, str) or queue_position
            setSqlTexts = []
            vals = ()
            if phase:
                setSqlTexts.append('`phase`=%s')
                vals += (phase,)
            if start_time:
                setSqlTexts.append('`time_start`=%s')
                vals += (start_time,)
            if end_time:
                setSqlTexts.append('`time_end`=%s')
                vals += (end_time,)
            if queue_position:
                setSqlTexts.append('`queue_position`=%s')
                ## A value of -1 indicates that the record value should be set to NULL
                queue_position_val = queue_position if queue_position >= 0 else None
                vals += (queue_position_val,)
            if isinstance(email, str):
                setSqlTexts.append('`email`=%s')
                vals += (email,)
            setSqlText = ', '.join(setSqlTexts)
            sql = f'''UPDATE `job` SET {setSqlText} WHERE `job_id`=%s'''
            log.debug(sql)
            log.debug(vals)
            vals += (job_id,)
            self.cur.execute((sql), vals)
            self.close_db_connection()
        except Exception as e:
            log.error(f'''Error updating job record: {e}''')
            self.close_db_connection()
            raise

    def select_registrants(self, user_id: str = '', approved: bool = None, fields: list = []) -> list:
        self.open_db_connection()
        try:
            criteria = '`id`!=0' # Dummy criterion in case no others are added
            if isinstance(approved, bool):
                approved_val = 1 if approved == True else 0
                criteria += f' AND `approved`={approved_val}'
            vals = ()
            if user_id:
                criteria += ' AND `user_id`=%s'
                vals += (user_id,)
            if isinstance(fields, list) and len(fields) > 0:
                fields = f'''`{'`, `'.join(fields)}`'''
            else:
                fields = '*'
            sql = f'''
                SELECT {fields} FROM `register`
                WHERE {criteria}
                ORDER BY `time_registered` DESC
            '''
            log.debug(f'sql: {sql}')
            log.debug(f'vals: {vals}')
            registrants = []
            self.cur.execute(sql, vals)
            col_names = [desc[0] for desc in self.cur.description]
            for record in self.cur.fetchall():
                registrant = {}
                idx = 0
                for col_val in record:
                    registrant[col_names[idx]] = col_val
                    idx += 1
                registrants.append(registrant)
            self.close_db_connection()
            return registrants
        except Exception as e:
            log.error(e)
            self.close_db_connection()
            raise

    def insert_registrant(self, user_info):
        self.open_db_connection()
        try:
            fields = {
                'user_id': user_info['user_id'],
                'name': user_info['name'],
                'email': user_info['email'],
                'time_registered': datetime.utcnow(),
            }
            placeholders = ', '.join(['%s'] * len(fields))
            columns = ', '.join(fields.keys())
            self.cur.execute(
                (f"INSERT INTO `register` ({columns}) VALUES ({placeholders})"),
                tuple(fields.values())
            )
            self.close_db_connection()
        except Exception as e:
            log.error(e)
            self.close_db_connection()
            raise


    def mark_registrant_approval_email_sent(self, user_id: str = '') -> None:
        self.open_db_connection()
        try:
            assert user_id
            vals = ()
            sql = f'''UPDATE `register` SET `email_sent`=%s WHERE `user_id`=%s'''
            vals += (True, user_id)
            log.debug(sql)
            log.debug(vals)
            self.cur.execute((sql), vals)
            self.close_db_connection()
        except Exception as e:
            log.error(f'''Error updating registrant record: {e}''')
            self.close_db_connection()
            raise


    def mark_registrant_approved(self, user_id: str = '') -> None:
        self.open_db_connection()
        try:
            assert user_id
            vals = ()
            sql = f'''UPDATE `register` SET `approved`=%s WHERE `user_id`=%s'''
            vals += (True, user_id)
            log.debug(sql)
            log.debug(vals)
            self.cur.execute((sql), vals)
            self.close_db_connection()
        except Exception as e:
            log.error(f'''Error updating registrant record: {e}''')
            self.close_db_connection()
            raise


    def insert_stats_jobs(self, job_stats) -> None:
        self.open_db_connection()
        try:
            fields = {
                'time_scanned': datetime.utcnow(),
                'num_total': job_stats['num_total'],
                'num_complete': job_stats['num_complete'],
                'num_error': job_stats['num_error'],
                'num_aborted': job_stats['num_aborted'],
                'avg_duration': job_stats['avg_duration'],
                'num_users': job_stats['num_users'],
                'dist_users': job_stats['dist_users'],
                'dist_user_agents': job_stats['dist_user_agents'],
            }
            placeholders = ', '.join(['%s'] * len(fields))
            columns = ', '.join(fields.keys())
            self.cur.execute(
                (f"INSERT INTO `stats_jobs` ({columns}) VALUES ({placeholders})"),
                tuple(fields.values())
            )
            self.close_db_connection()
        except Exception as e:
            log.error(e)
            self.close_db_connection()
            raise
