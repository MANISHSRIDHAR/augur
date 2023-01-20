import sys
import json
import time
import traceback
import logging
import platform
import imp
import time
import datetime
import html.parser
import subprocess
import os
import getopt
import xlsxwriter
import configparser
import multiprocessing
import numpy as np
from celery import group, chain, chord, signature
from celery.utils.log import get_task_logger
from celery.result import allow_join_result
from celery.signals import after_setup_logger
from datetime import timedelta
import sqlalchemy as s


from augur.tasks.git.util.facade_worker.facade_worker.facade02utilitymethods import update_repo_log, trim_commit, store_working_author, trim_author
from augur.tasks.git.util.facade_worker.facade_worker.facade03analyzecommit import analyze_commit
from augur.tasks.github.facade_github.tasks import *

from augur.tasks.util.worker_util import create_grouped_task_load

from augur.tasks.init.celery_app import celery_app as celery


from augur.application.db import data_parse
from augur.tasks.util.AugurUUID import GithubUUID, UnresolvableUUID
from augur.application.db.models import PullRequest, Message, PullRequestReview, PullRequestLabel, PullRequestReviewer, PullRequestEvent, PullRequestMeta, PullRequestAssignee, PullRequestReviewMessageRef, Issue, IssueEvent, IssueLabel, IssueAssignee, PullRequestMessageRef, IssueMessageRef, Contributor, Repo

from augur.tasks.github.util.github_paginator import GithubPaginator, hit_api
from augur.tasks.github.util.gh_graphql_entities import PullRequest
from augur.tasks.github.util.github_task_session import *

from augur.application.logs import TaskLogConfig

#define an error callback for chains in facade collection so facade doesn't make the program crash
#if it does.
@celery.task
def facade_error_handler(request,exc,traceback):
    logger = logging.getLogger(facade_error_handler.__name__)

    logger.error(f"Task {request.id} raised exception: {exc}! \n {traceback}")

    print(f"chain: {request.chain}")
    #Make sure any further execution of tasks dependent on this one stops.
    try:
        #Replace the tasks queued ahead of this one in a chain with None.
        request.chain = None
    except AttributeError:
        pass #Task is not part of a chain. Normal so don't log.
    except Exception as e:
        logger.error(f"Could not mutate request chain! \n Error: {e}")


#Predefine facade collection with tasks
@celery.task
def facade_analysis_init_facade_task():
    logger = logging.getLogger(facade_analysis_init_facade_task.__name__)
    with FacadeSession(logger) as session:
        session.update_status('Running analysis')
        session.log_activity('Info',f"Beginning analysis.")

@celery.task
def grab_comitters(repo_id_list,platform="github"):
    logger = logging.getLogger(grab_comitters.__name__)

    for repo_id in repo_id_list:
        try:
            grab_committer_list(GithubTaskSession(logger), repo_id,platform)
        except Exception as e:
            logger.error(f"Could not grab committers from github endpoint!\n Reason: {e} \n Traceback: {''.join(traceback.format_exception(None, e, e.__traceback__))}")


@celery.task
def trim_commits_facade_task(repo_id_list):
    logger = logging.getLogger(trim_commits_facade_task.__name__)
    session = FacadeSession(logger)

    def update_analysis_log(repos_id,status):

    # Log a repo's analysis status

        log_message = s.sql.text("""INSERT INTO analysis_log (repos_id,status)
            VALUES (:repo_id,:status)""").bindparams(repo_id=repos_id,status=status)

        try:
            session.execute_sql(log_message)
        except:
            pass

    for repo_id in repo_id_list:

        session.inc_repos_processed()
        update_analysis_log(repo_id,"Beginning analysis.")
        # First we check to see if the previous analysis didn't complete

        get_status = s.sql.text("""SELECT working_commit FROM working_commits WHERE repos_id=:repo_id
            """).bindparams(repo_id=repo_id)

        try:
            working_commits = session.fetchall_data_from_sql_text(get_status)
        except:
            working_commits = []

        # If there's a commit still there, the previous run was interrupted and
        # the commit data may be incomplete. It should be trimmed, just in case.
        for commit in working_commits:
            trim_commit(session, repo_id,commit['working_commit'])

            # Remove the working commit.
            remove_commit = s.sql.text("""DELETE FROM working_commits
                WHERE repos_id = :repo_id AND 
                working_commit = :commit""").bindparams(repo_id=repo_id,commit=commit['working_commit'])
            session.execute_sql(remove_commit)
            session.log_activity('Debug',f"Removed working commit: {commit['working_commit']}")

        # Start the main analysis

        update_analysis_log(repo_id,'Collecting data')
        logger.info(f"Got past repo {repo_id}")

@celery.task
def trim_commits_post_analysis_facade_task(repo_ids):
    logger = logging.getLogger(trim_commits_post_analysis_facade_task.__name__)
    

    session = FacadeSession(logger)
    start_date = session.get_setting('start_date')
    def update_analysis_log(repos_id,status):

        # Log a repo's analysis status

        log_message = s.sql.text("""INSERT INTO analysis_log (repos_id,status)
            VALUES (:repo_id,:status)""").bindparams(repo_id=repos_id,status=status)

        
        session.execute_sql(log_message)
    
    for repo_id in repo_ids:
        session.logger.info(f"Generating sequence for repo {repo_id}")

        query = session.query(Repo).filter(Repo.repo_id == repo_id)
        repo = execute_session_query(query, 'one')

        #Get the huge list of commits to process.
        repo_loc = (f"{session.repo_base_directory}{repo.repo_group_id}/{repo.repo_path}{repo.repo_name}/.git")
        # Grab the parents of HEAD

        parents = subprocess.Popen(["git --git-dir %s log --ignore-missing "
        "--pretty=format:'%%H' --since=%s" % (repo_loc,start_date)],
        stdout=subprocess.PIPE, shell=True)

        parent_commits = set(parents.stdout.read().decode("utf-8",errors="ignore").split(os.linesep))

        # If there are no commits in the range, we still get a blank entry in
        # the set. Remove it, as it messes with the calculations

        if '' in parent_commits:
            parent_commits.remove('')

        # Grab the existing commits from the database

        existing_commits = set()

        find_existing = s.sql.text("""SELECT DISTINCT cmt_commit_hash FROM commits WHERE repo_id=:repo_id
            """).bindparams(repo_id=repo_id)

        #session.cfg.cursor.execute(find_existing, (repo[0], ))

        try:
            for commit in session.fetchall_data_from_sql_text(find_existing):#list(session.cfg.cursor):
                existing_commits.add(commit['cmt_commit_hash'])
        except:
            session.log_activity('Info', 'list(cfg.cursor) returned an error')

        # Find missing commits and add them

        missing_commits = parent_commits - existing_commits

        session.log_activity('Debug',f"Commits missing from repo {repo_id}: {len(missing_commits)}")
        
        if len(missing_commits) > 0:
            #session.log_activity('Info','Type of missing_commits: %s' % type(missing_commits))

            #encode the repo_id with the commit.
            commits_with_repo_tuple = [(commit,repo_id) for commit in list(missing_commits)]
            #Get all missing commits into one large list to split into task pools
            all_missing_commits.extend(commits_with_repo_tuple)
        
        # Find commits which are out of the analysis range

        trimmed_commits = existing_commits - parent_commits

        update_analysis_log(repo_id,'Data collection complete')

        update_analysis_log(repo_id,'Beginning to trim commits')

        session.log_activity('Debug',f"Commits to be trimmed from repo {repo_id}: {len(trimmed_commits)}")

    
    
        for commit in trimmed_commits:
            trim_commit(session,repo_id,commit)
        
        set_complete = s.sql.text("""UPDATE repo SET repo_status='Complete' WHERE repo_id=:repo_id and repo_status != 'Empty'
            """).bindparams(repo_id=repo_id)

        session.execute_sql(set_complete)

        update_analysis_log(repo_id,'Commit trimming complete')

        update_analysis_log(repo_id,'Complete')
    


@celery.task
def facade_analysis_end_facade_task():
    logger = logging.getLogger(facade_analysis_end_facade_task.__name__)
    FacadeSession(logger).log_activity('Info','Running analysis (complete)')



@celery.task
def facade_start_contrib_analysis_task():
    logger = logging.getLogger(facade_start_contrib_analysis_task.__name__)
    session = FacadeSession(logger)
    session.update_status('Updating Contributors')
    session.log_activity('Info', 'Updating Contributors with commits')


#enable celery multithreading
@celery.task
def analyze_commits_in_parallel(repo_ids, multithreaded: bool)-> None:
    """Take a large list of commit data to analyze and store in the database. Meant to be run in parallel with other instances of this task.
    """

    #create new session for celery thread.
    logger = logging.getLogger(analyze_commits_in_parallel.__name__)
    session = FacadeSession(logger)
    start_date = session.get_setting('start_date')

    for repo_id in repo_ids:
        session.logger.info(f"Generating sequence for repo {repo_id}")
        

        query = session.query(Repo).filter(Repo.repo_id == repo_id)
        repo = execute_session_query(query, 'one')

        #Get the huge list of commits to process.
        repo_loc = (f"{session.repo_base_directory}{repo.repo_group_id}/{repo.repo_path}{repo.repo_name}/.git")
        # Grab the parents of HEAD

        parents = subprocess.Popen(["git --git-dir %s log --ignore-missing "
        "--pretty=format:'%%H' --since=%s" % (repo_loc,start_date)],
        stdout=subprocess.PIPE, shell=True)

        parent_commits = set(parents.stdout.read().decode("utf-8",errors="ignore").split(os.linesep))

        # If there are no commits in the range, we still get a blank entry in
        # the set. Remove it, as it messes with the calculations

        if '' in parent_commits:
            parent_commits.remove('')

        # Grab the existing commits from the database

        existing_commits = set()

        find_existing = s.sql.text("""SELECT DISTINCT cmt_commit_hash FROM commits WHERE repo_id=:repo_id
            """).bindparams(repo_id=repo_id)

        #session.cfg.cursor.execute(find_existing, (repo[0], ))

        try:
            for commit in session.fetchall_data_from_sql_text(find_existing):#list(session.cfg.cursor):
                existing_commits.add(commit['cmt_commit_hash'])
        except:
            session.log_activity('Info', 'list(cfg.cursor) returned an error')

        # Find missing commits and add them

        missing_commits = parent_commits - existing_commits

        session.log_activity('Debug',f"Commits missing from repo {repo_id}: {len(missing_commits)}")
        
        queue = []
        if len(missing_commits) > 0:
            #session.log_activity('Info','Type of missing_commits: %s' % type(missing_commits))

            #encode the repo_id with the commit.
            commits = [commit for commit in list(missing_commits)]
            #Get all missing commits into one large list to split into task pools
            queue.extend(commits)
        else:
            return

        logger.info(f"Got to analysis!")
        
        for count, commitTuple in enumerate(queue):

            #Log progress when another quarter of the queue has been processed
            if (count + 1) % int(len(queue) / 4) == 0:
                logger.info(f"Progress through current analysis queue is {(count / len(queue)) * 100}%")

            query = session.query(Repo).filter(Repo.repo_id == repo_id)
            repo = execute_session_query(query,'one')


            repo_loc = (f"{session.repo_base_directory}{repo.repo_group_id}/{repo.repo_path}{repo.repo_name}/.git")    

            analyze_commit(session, repo_id, repo_loc, commitTuple)

    logger.info("Analysis complete")
    return

@celery.task
def nuke_affiliations_facade_task():
    logger = logging.getLogger(nuke_affiliations_facade_task.__name__)
    session = FacadeSession(logger)

    nuke_affiliations(session)

@celery.task
def fill_empty_affiliations_facade_task():
    logger = logging.getLogger(fill_empty_affiliations_facade_task.__name__)
    with FacadeSession(logger) as session:
        fill_empty_affiliations(session)

@celery.task
def invalidate_caches_facade_task():
    logger = logging.getLogger(invalidate_caches_facade_task.__name__)

    with FacadeSession(logger) as session:
        invalidate_caches(session)

@celery.task
def rebuild_unknown_affiliation_and_web_caches_facade_task():
    logger = logging.getLogger(rebuild_unknown_affiliation_and_web_caches_facade_task.__name__)
    
    with FacadeSession(logger) as session:
        rebuild_unknown_affiliation_and_web_caches(session)



def generate_analysis_sequence(logger,repo_git_identifiers):
    """Run the analysis by looping over all active repos. For each repo, we retrieve
    the list of commits which lead to HEAD. If any are missing from the database,
    they are filled in. Then we check to see if any commits in the database are
    not in the list of parents, and prune them out.

    We also keep track of the last commit to be processed, so that if the analysis
    is interrupted (possibly leading to partial data in the database for the
    commit being analyzed at the time) we can recover.
    """

    
    
    analysis_sequence = []

    with FacadeSession(logger) as session:
        repo_list = s.sql.text("""SELECT repo_id,repo_group_id,repo_path,repo_name FROM repo 
        WHERE repo_git IN :values""").bindparams(values=tuple(repo_git_identifiers))
        repos = session.fetchall_data_from_sql_text(repo_list)

        start_date = session.get_setting('start_date')

        repo_ids = [repo['repo_id'] for repo in repos]

        #determine amount of celery tasks to run at once in each grouped task load
        concurrentTasks = int((-1 * (15/(len(repo_ids)+1))) + 15)
        logger.info(f"Scheduling concurrent layers {concurrentTasks} tasks at a time.")

        analysis_sequence.append(facade_analysis_init_facade_task.si())

        analysis_sequence.append(create_grouped_task_load(dataList=repo_ids,task=grab_comitters,processes=concurrentTasks))

        analysis_sequence.append(create_grouped_task_load(dataList=repo_ids,task=trim_commits_facade_task,processes=concurrentTasks))

        analysis_sequence.append(create_grouped_task_load(True,dataList=repo_ids,task=analyze_commits_in_parallel,processes=concurrentTasks))

        analysis_sequence.append(create_grouped_task_load(dataList=repo_ids,task=trim_commits_post_analysis_facade_task,processes=concurrentTasks))

        
        analysis_sequence.append(facade_analysis_end_facade_task.si())
    
    logger.info(f"Analysis sequence: {analysis_sequence}")
    return analysis_sequence



def generate_contributor_sequence(logger,repo_git_identifiers):
    
    contributor_sequence = []
    all_repo_ids = []
    with FacadeSession(logger) as session:
        
        #contributor_sequence.append(facade_start_contrib_analysis_task.si())
        query = s.sql.text("""SELECT repo_id FROM repo
        WHERE repo_git IN :values""").bindparams(values=tuple(repo_git_identifiers))

        all_repos = session.fetchall_data_from_sql_text(query)
        #pdb.set_trace()
        #breakpoint()
        #for repo in all_repos:
        #    contributor_sequence.append(insert_facade_contributors.si(repo['repo_id']))
        all_repo_ids = [repo['repo_id'] for repo in all_repos]

    contrib_group = create_grouped_task_load(dataList=all_repo_ids,task=insert_facade_contributors)#group(contributor_sequence)
    contrib_group.link_error(facade_error_handler.s())
    return contrib_group#chain(facade_start_contrib_analysis_task.si(), contrib_group)




def generate_facade_chain(logger,repo_git_identifiers):
    #raise NotImplemented

    logger.info("Generating facade sequence")
    with FacadeSession(logger) as session:
        
        # Figure out what we need to do
        limited_run = session.limited_run
        delete_marked_repos = session.delete_marked_repos
        pull_repos = session.pull_repos
        clone_repos = session.clone_repos
        check_updates = session.check_updates
        force_updates = session.force_updates
        run_analysis = session.run_analysis
        force_analysis = session.force_analysis
        nuke_stored_affiliations = session.nuke_stored_affiliations
        fix_affiliations = session.fix_affiliations
        force_invalidate_caches = session.force_invalidate_caches
        rebuild_caches = session.rebuild_caches
        #if abs((datetime.datetime.strptime(session.cfg.get_setting('aliases_processed')[:-3], 
            # '%Y-%m-%d %I:%M:%S.%f') - datetime.datetime.now()).total_seconds()) // 3600 > int(session.cfg.get_setting(
            #   'update_frequency')) else 0
        force_invalidate_caches = session.force_invalidate_caches
        create_xlsx_summary_files = session.create_xlsx_summary_files
        multithreaded = session.multithreaded

        facade_sequence = []

        if not limited_run or (limited_run and delete_marked_repos):
            git_repo_cleanup(session,repo_git_identifiers)

        if not limited_run or (limited_run and clone_repos):
            git_repo_initialize(session,repo_git_identifiers)

        if not limited_run or (limited_run and check_updates):
            check_for_repo_updates(session,repo_git_identifiers)

        if force_updates:
            force_repo_updates(session,repo_git_identifiers)#facade_sequence.append(force_repo_updates_facade_task.si())

        if not limited_run or (limited_run and pull_repos):
            git_repo_updates(session,repo_git_identifiers)#facade_sequence.append(git_repo_updates_facade_task.si())

        if force_analysis:
            force_repo_analysis(session,repo_git_identifiers)#facade_sequence.append(force_repo_analysis_facade_task.si())

        #Generate commit analysis task order.
        facade_sequence.extend(generate_analysis_sequence(logger,repo_git_identifiers))

        #Generate contributor analysis task group.
        facade_sequence.append(generate_contributor_sequence(logger,repo_git_identifiers))

        if nuke_stored_affiliations:
            facade_sequence.append(nuke_affiliations_facade_task.si().on_error(facade_error_handler.s()))#nuke_affiliations(session.cfg)

        #session.logger.info(session.cfg)
        if not limited_run or (limited_run and fix_affiliations):
            facade_sequence.append(fill_empty_affiliations_facade_task.si().on_error(facade_error_handler.s()))#fill_empty_affiliations(session)

        if force_invalidate_caches:
            facade_sequence.append(invalidate_caches_facade_task.si().on_error(facade_error_handler.s()))#invalidate_caches(session.cfg)

        if not limited_run or (limited_run and rebuild_caches):
            facade_sequence.append(rebuild_unknown_affiliation_and_web_caches_facade_task.si().on_error(facade_error_handler.s()))#rebuild_unknown_affiliation_and_web_caches(session.cfg)
        
        logger.info(f"Facade sequence: {facade_sequence}")
        return chain(*facade_sequence)

