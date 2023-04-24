#---
CREATE TABLE IF NOT EXISTS `meta`(
    `Lock` char(1) NOT NULL DEFAULT 'X',
    `schema_version` int NOT NULL DEFAULT 0,
    constraint PK_meta PRIMARY KEY (`Lock`),
    constraint CK_meta_Locked CHECK (`Lock`='X')
)
#---
CREATE TABLE IF NOT EXISTS `job`(
    `id` int NOT NULL AUTO_INCREMENT,
    `job_id` varchar(32) NOT NULL,
    `run_id` varchar(64) NOT NULL,
    `user_id` varchar(50) NOT NULL,
    `command` text NOT NULL DEFAULT '',
    `type` varchar(50) NOT NULL,
    `phase` varchar(50) NOT NULL,
    `time_created` datetime NOT NULL DEFAULT 0,
    `time_start` datetime NOT NULL DEFAULT 0,
    `time_end` datetime NOT NULL DEFAULT 0,
    `user_agent` varchar(256) NOT NULL DEFAULT '',
    `email` varchar(128) NOT NULL DEFAULT '',
    `job_info` text NOT NULL DEFAULT '',
    `deleted` boolean NOT NULL DEFAULT 0,
    `queue_position` int,
    PRIMARY KEY (`id`), UNIQUE KEY `id` (`id`), UNIQUE KEY `job_id` (`job_id`)
)
#---
CREATE TABLE IF NOT EXISTS `stats_jobs`(
    `id` int NOT NULL AUTO_INCREMENT,
    `time_scanned` datetime NOT NULL,
    `num_total` integer NOT NULL,
    `num_complete` integer NOT NULL,
    `num_error` integer NOT NULL,
    `num_aborted` integer NOT NULL,
    `avg_duration` integer NOT NULL,
    `num_users` integer NOT NULL,
    `dist_users` text NOT NULL,
    `dist_user_agents` text NOT NULL,
    PRIMARY KEY (`id`), UNIQUE KEY `id` (`id`)
)
#---
CREATE TABLE IF NOT EXISTS `mailing_list`(
    `email` varchar(320) NOT NULL,
    `time_created` datetime NOT NULL DEFAULT 0,
    PRIMARY KEY (`email`), UNIQUE KEY `email` (`email`)
)