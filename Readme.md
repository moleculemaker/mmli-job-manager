# API server

## Requirements

The job system requires a Persistent Volume Claim (PVC) to store job output. This can be created (if it does not already exist) using Helm:

```bash
$ helm upgrade --install --namespace job-manager job-manager chart/

Release "job-manager" has been upgraded. Happy Helming!
NAME: job-manager
LAST DEPLOYED: Thu Mar  9 12:49:08 2023
NAMESPACE: job-manager
STATUS: deployed
REVISION: 2
TEST SUITE: None
```

## Instructions

1. Copy your MMLI Kubernetes config file to `kubeconfig` in your git clone root directory.
1. Copy the `env.tpl` file to `.env`.
1. Invoke Docker Compose to bootstrap the database and run the job-manager:
    ```bash
    docker compose up --build -d
    # Optionally follow logs:
    docker compose logs -f
    ```
1. In a separate terminal, open a terminal in the container and use the test script to launch a job:
    ```bash
    $ docker compose exec -it app bash

    worker@c46604b1361d:~/src$ cd ~/test/

    worker@c46604b1361d:~/test$ python3 job_cannon.py --num 5
    Submitting new MMLI job (job_config.yaml)...
    Job "970d69983e8c46deb0fbb301a4bdf77c" created.
    ...
    ```
1. List the job pods and view their log output:
    ```bash
    $ kubectl get pod -n job-manager -l type=uws-job 
    NAME                                             READY   STATUS      RESTARTS   AGE
    uws-job-753871dc99434e75a20b55df1b7a0401-jnbsx   0/1     Completed   0          78s
    uws-job-7cb9be127c1c48a29164aafc1552b611-phthg   0/1     Completed   0          78s
    uws-job-ab548ff18f0b488bac5ac2e00b3f5186-b7t8r   0/1     Completed   0          78s
    uws-job-befa23c3527045388c2e52832f4a943a-4npsb   0/1     Completed   0          78s
    uws-job-e36763a771964ac6bcff6f3f8a0a168c-ph7s6   0/1     Completed   0          78s

    $ kubectl logs -n job-manager -c job -l type=uws-job 
    Hello MMLI!
    Hello MMLI!
    Hello MMLI!
    Hello MMLI!
    Hello MMLI!
    ```
1. Log into MariaDB instance and query `job` table:
    ```bash
    $ docker compose exec -it db bash

    I have no name!@cdb5aa512ecc:/$ mysql -u$MARIADB_USER -p$MARIADB_PASSWORD $MARIADB_DATABASE
    ...
    MariaDB [mmli]> select id,job_id,time_created from job;
    +----+----------------------------------+---------------------+
    | id | job_id                           | time_created        |
    +----+----------------------------------+---------------------+
    |  1 | d6c1e96b2351491c9fccd9360f612b92 | 2023-03-09 18:21:32 |
    |  2 | 5970a4c3b0134ee4bac9cd678b3394f2 | 2023-03-09 18:32:39 |
    |  3 | bcc12ad61a5c436fa51185d2a0b034dc | 2023-03-09 18:33:54 |
    |  4 | 076baf20ebc540c08a5375ece33a78c6 | 2023-03-09 18:35:53 |
    ...
    ```

## Configuration

If you want to override the configuration set in `config/server.yaml` without modifying that file, you can create `config/secret.yaml` (which is in `.gitignore`). For example to override the log level you would populate `config/secret.yaml` with the following:

```yaml
server:
  logLevel: "INFO"
```
