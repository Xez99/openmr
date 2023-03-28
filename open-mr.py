#!/usr/bin/env python3

import json
import sys
import http.client as http
import subprocess
import re
import os

###
# COLOR CONSTANTS
###
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

###
# CONFIGURABLE
###
JIRA_HOST = 'jira.atlassian.com'
JIRA_API_VERSION = '2'
GITLAB_HOST = 'gitlab.com'
GITLAB_API_VERSION = 'v4'
TARGET_BRANCH = 'develop' #TODO replace with repo default branch
TASK_NAME_REGEX='TCRM-[0-9]*'
SQUASH = True
REMOVE_SOURCE_BRANCH = True

if 'JIRA_TOKEN' not in os.environ:
    print(f'{RED}There is no "JIRA_TOKEN" in ENV variables! Exiting...{NC}')
    exit(1)
JIRA_TOKEN = os.environ['JIRA_TOKEN']

if 'GITLAB_TOKEN' not in os.environ:
    print(f'{RED}There is no "GITLAB_TOKEN" in ENV variables! Exiting...{NC}')
    exit(1)
GITLAB_TOKEN = os.environ['GITLAB_TOKEN']


def get_current_branch():
    return subprocess.run(['git', 'branch', '--show-current'], capture_output=True, text=True).stdout.strip('\n')

def send_https_request(method, host, path, headers, body = '', expected_status=200):
    connection = http.HTTPSConnection(host)
    try:
        connection.request(method, path, body, headers = headers)
        response = connection.getresponse()
        if(response.status != expected_status):
            raise Exception(
                f'{RED}Request failed{NC}\n' \
                f'request: {method} https://{host}{path}\n{body}\n\n' \
                f'response status: {str(response.status)}\n' \
                f'response body: {response.read().decode("utf-8")}'
            )
        return json.load(response)

    finally:
        connection.close()

def get_project_id():
    remote_url = subprocess.run(['git', 'remote', 'get-url', 'origin'], capture_output=True, text=True).stdout.strip('\n')
    project_name = re.search('/([a-zA-Z0-9\-]*)\.git', remote_url).group(1)
    min_access_level = '30' # Developer
    max_per_page = '100' # Gitlab API max. FIXME: because of max size per page there is some chance of not finding the project in large repos
    response = send_https_request(
        'GET',
        GITLAB_HOST,
        f'/api/{GITLAB_API_VERSION}/projects?' \
            'archived=false' \
            f'&min_access_level={min_access_level}' \
            '&simple=true' \
            '&with_merge_requests_enabled=true' \
            '&pagination=keyset' \
            f'&per_page={max_per_page}' \
            '&sort=desc' \
            '&order_by=id' \
            f'&search={project_name}',
        headers = {'PRIVATE-TOKEN': GITLAB_TOKEN},
    )

    for project in response:
        if remote_url in [ project['ssh_url_to_repo'], project['http_url_to_repo'] ]:
            return project['id']

    raise Exception(f'{RED}Cannot find project whith url:{NC} {remote_url}')


def get_user_id():
    return send_https_request(
        'GET',
        GITLAB_HOST,
        f'/api/{GITLAB_API_VERSION}/user',
        headers = {'PRIVATE-TOKEN': GITLAB_TOKEN}
    )['id']

def get_already_exist_mr_link(project_id, current_branch, target_branch):
    response = send_https_request(
        'GET',
        GITLAB_HOST,
        f'/api/{GITLAB_API_VERSION}/projects/{project_id}/merge_requests?' \
            'state=opened'\
            f'&source_branch={current_branch}' \
            f'&target_branch={target_branch}',
        headers = {'PRIVATE-TOKEN': GITLAB_TOKEN, 'Content-Type': 'application/json'}
    )
    if len(response) > 0:
        return response[0]['web_url']
    else:
        return None

def create_mr_request_body(title, current_branch, target_branch, user_id, squash=True, remove_source_branch=True):
    return json.dumps({
        'title': title,
        'source_branch': current_branch,
        'target_branch': target_branch,
        'assignee_id': user_id,
        'squash': squash,
        'remove_source_branch': remove_source_branch
    })

def create_mr(project_id, title, current_branch, target_branch, user_id, squash=True, remove_source_branch=True):
    return send_https_request(
        'POST',
        GITLAB_HOST,
        f'/api/{GITLAB_API_VERSION}/projects/{project_id}/merge_requests',
        body = create_mr_request_body(title, current_branch, target_branch, user_id, squash, remove_source_branch),
        headers = {'PRIVATE-TOKEN': GITLAB_TOKEN, 'Content-Type': 'application/json'},
        expected_status = 201
    )['web_url']

def get_task_title(task):
    return send_https_request(
        'GET',
        JIRA_HOST,
        f'/rest/api/{JIRA_API_VERSION}/issue/{task}',
        headers = {'Authorization': f'Bearer {JIRA_TOKEN}'}
    )['fields']['summary']

def get_service_name_from(mr_link):
    return re.search('\/([a-zA-Z\-]*)\/-\/merge_requests', mr_link).group(1)

def is_issue_link_already_exist(task, mr_link):
    response = send_https_request(
        'GET',
        JIRA_HOST,
        f'/rest/api/{JIRA_API_VERSION}/issue/{task}/remotelink',
        headers = {'Authorization': f'Bearer {JIRA_TOKEN}'}
    )

    for issue in response:
        if issue['object']['url'] == mr_link:
            return True
    return False

def create_jira_issue_link_request_body(mr_link):
    return json.dumps({
        'object': {
            'url': mr_link,
            'title': get_service_name_from(mr_link),
            'icon': {
                'url16x16': f'https://{GITLAB_HOST}/favicon.ico'
            },
        }
    })

def add_jira_issue_link(task, mr_link):
    return send_https_request(
        'POST',
        JIRA_HOST,
        f'/rest/api/{JIRA_API_VERSION}/issue/{task}/remotelink',
        body = create_jira_issue_link_request_body(mr_link),
        headers = {'Authorization': f'Bearer {JIRA_TOKEN}', 'Content-Type': 'application/json'},
        expected_status = 201
    )

def main():
    print('>>> Geting current branch name...')
    current_branch = get_current_branch()
    print(current_branch)
    
    if current_branch == TARGET_BRANCH:
        print(f'{RED}Cannot open MR:{NC} current branch is target ({TARGET_BRANCH})')
        exit(1)
    
    print('>>> Extracting task name from branch name...')
    match = re.match(TASK_NAME_REGEX, current_branch) # Validate branch name format
    if not match:
        print(f"{RED}Cannot open MR:{NC} current branch name ({TARGET_BRANCH}) doesn't start with 'TCRM-[0-9]*'")
        exit(1)
    task = match.group()
    print(task)

    print('>>> Getting GitLab project id...')
    project_id = get_project_id()
    print(project_id)
    
    print('>>> Check if MR is already oppened...')
    mr_link = get_already_exist_mr_link(project_id, current_branch, TARGET_BRANCH)

    if mr_link is not None:
        print(f'MR already opened:\n{BLUE}{mr_link}{NC}')
    else:
        print('No open MR was found. A new one will be created')
        
        print('>>> Getting task title...')
        title = task + get_task_title(task)
        print(title)

        print('>>> Creating MR...')
        mr_link = create_mr(
            project_id,
            title,
            current_branch,
            TARGET_BRANCH,
            get_user_id(),
            SQUASH,
            REMOVE_SOURCE_BRANCH
        )
        print(f'{GREEN}MR opened:{NC} {current_branch}')
        print(f'{BLUE}{mr_link}{NC}')
    
    print('>>> Check if MR link in task is already exist...')
    if is_issue_link_already_exist(task, mr_link):
        print('MR link already added to task')
    else:
        print('MR link not found')
        print('>>> Adding MR link to task...')
        add_jira_issue_link(task, mr_link)
        print(f'{GREEN}MR link added to task{NC}')
    
    print(f'{BLUE}https://{JIRA_HOST}/browse/{task}{NC}')

if __name__ == "__main__":
    main()
