import os
import subprocess
import requests
import json
import logging
from datetime import datetime
import base64
import time
from urllib.parse import quote
import argparse

CHECKPOINT_FILE = os.path.join(os.getcwd(), "tfs_git_checkpoint.json")

def setup_logging(log_dir):
    """Sets up logging to a new file with the current date and time."""
    log_filename = datetime.now().strftime("script_log_%Y-%m-%d_%H-%M-%S.log")
    log_path = os.path.join(log_dir, log_filename)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s]: %(message)s",
        handlers=[
            logging.FileHandler(log_path, mode='w'),
            logging.StreamHandler()
        ]
    )
    logging.info(f"Logging initialized: {log_path}")

def load_config():
    """Loads configuration from config.json."""
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r") as config_file:
        return json.load(config_file)

def get_auth_header(pat):
    """Returns the authorization header for Azure DevOps."""
    auth_str = f":{pat}".encode("utf-8")
    auth_b64 = base64.b64encode(auth_str).decode("utf-8")
    return {"Authorization": f"Basic {auth_b64}"}

def get_github_auth_header(token):
    """Returns the authorization header for GitHub."""
    return {"Authorization": f"token {token}"}

def save_checkpoint(collection_name, project_name, repo_name):
    """Saves migrated repository name under a JSON hierarchy in tfs_git_checkpoint.json."""
    checkpoint = {}
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r") as file:
            checkpoint = json.load(file)

    if collection_name not in checkpoint:
        checkpoint[collection_name] = {}

    if project_name not in checkpoint[collection_name]:
        checkpoint[collection_name][project_name] = []

    if repo_name not in checkpoint[collection_name][project_name]:
        checkpoint[collection_name][project_name].append(repo_name)
        with open(CHECKPOINT_FILE, "w") as file:
            json.dump(checkpoint, file, indent=4)
        logging.info(f"Checkpoint updated: {repo_name} saved under {collection_name}/{project_name}.")

def load_checkpoint():
    """Loads the tfs_git_checkpoint.json file, creating it if it doesn't exist."""
    if not os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "w") as file:
            json.dump({}, file)  # Create an empty JSON object
    with open(CHECKPOINT_FILE, "r") as file:
        return json.load(file)

def retry_subprocess(command, retries=3):
    """Retries a subprocess command in case of network issues."""
    for attempt in range(retries):
        try:
            subprocess.run(command, check=True)
            return
        except subprocess.CalledProcessError as e:
            logging.warning(f"Command failed: {command}, retry {attempt + 1}/{retries}")
            time.sleep(2 ** attempt)
    raise Exception(f"Command failed after {retries} retries: {command}")

def migrate_specific_branches(repo_dir, github_repo_url, branches):
    """Fetches and pushes specific branches."""
    for branch in branches:
        logging.info(f"Pushing specific branch: {branch}")
        retry_subprocess(["git", "fetch", "origin", branch])
        retry_subprocess(["git", "push", github_repo_url, f"refs/remotes/origin/{branch}:refs/heads/{branch}"])

def create_directory_structure(base_dir, org, project):
    """Creates the required directory structure."""
    org_dir = os.path.join(base_dir, org)
    project_dir = os.path.join(org_dir, project)
    os.makedirs(project_dir, exist_ok=True)
    return project_dir

def check_github_repo_exists(github_org, repo_name, github_token):
    """Checks if a repository exists on GitHub."""
    url = f"https://api.github.com/repos/{github_org}/{repo_name}"
    headers = get_github_auth_header(github_token)
    response = requests.get(url, headers=headers)
    print(response)
    return response.status_code == 200

def clone_and_push_repositories(config, only_clone_repos):
    """Clones repositories from Azure DevOps and pushes them to GitHub."""
    azure_org = config["azure_devops_organization"]
    azure_project = config["azure_devops_project"]
    azure_pat = config["azure_devops_pat_token"]
    tfs_repo = config["tfs_source_repo"]
    git_repo = config["github_target_repo"]
    tfs_domain = config["tfs_url"]
    collection_name = config["azure_devops_organization"]
    github_token = config["github_token"]
    specific_branches = config.get("specific_branches", "all")  # Default to "all"

    # Create the main directory structure
    base_dir = os.path.join(os.getcwd(), "tfs_repo_migration")
    repo_base_dir = os.path.join(base_dir, "tfs_git_repo")
    log_dir = os.path.join(base_dir, "tfs_git_migration_log")
    os.makedirs(repo_base_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    # Setup logging
    setup_logging(log_dir)

    # Create organization and project directories
    project_dir = create_directory_structure(repo_base_dir, azure_org, azure_project)

    url = f"{tfs_domain}/{azure_org}/{azure_project}/_apis/git/repositories?api-version=5.0"
    headers = get_auth_header(azure_pat)
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        raise Exception(f"Failed to fetch repositories: {response.text}")

    repositories = response.json().get("value", [])
    repo_names = [repo["name"] for repo in repositories]

    # Check if the specified repository exists
    if tfs_repo not in repo_names:
        raise Exception(f"Repository {tfs_repo} not found in the given TFS collection.")

    checkpoint = load_checkpoint()

    repo = next((r for r in repositories if r["name"] == tfs_repo), None)
    if repo:
        repo_name = repo["name"]

        if collection_name in checkpoint and azure_project in checkpoint[collection_name] and repo_name in checkpoint[collection_name][azure_project]:
            logging.info(f"Skipping {repo_name}, already migrated.")
            return

        # Ensure the repository exists on GitHub
        if not check_github_repo_exists(config['github_organization'], git_repo, github_token):
            logging.error(f"Repository {git_repo} does not exist on GitHub. Migration cannot proceed.")
            return

        repo_dir = os.path.join(project_dir, repo_name)
        azure_repo_url = f"{tfs_domain}/{azure_org}/{azure_project}/_git/{quote(repo_name)}"

        try:
            if not os.path.exists(repo_dir):
                if specific_branches == "all":
                    logging.info(f"Cloning all branches for {repo_name}...")
                    retry_subprocess(["git", "clone", "--mirror", azure_repo_url, repo_dir])
                else:
                    logging.info(f"Cloning default branch for {repo_name}...")
                    retry_subprocess(["git", "clone", azure_repo_url, repo_dir])

            if not only_clone_repos:
                os.chdir(repo_dir)
                github_repo_url = f"https://github.com/{config['github_organization']}/{quote(git_repo)}.git"

                if specific_branches == "all":
                    logging.info(f"Pushing all branches and tags for {repo_name}...")
                    retry_subprocess(["git", "push", "--mirror", github_repo_url])
                else:
                    logging.info(f"Pushing selected branches: {specific_branches} for {repo_name}")
                    migrate_specific_branches(repo_dir, github_repo_url, specific_branches)

        except Exception as e:
            logging.error(f"An error occurred while processing {repo_name}: {e}")

        finally:
            # Save checkpoint regardless of success or failure
            save_checkpoint(collection_name, azure_project, repo_name)
            os.chdir(base_dir)

def main():
    parser = argparse.ArgumentParser(description="TFS (GIT) to GitHub migration script.")
    parser.add_argument("--only_clone_repos", action="store_true", help="Only clone repositories without pushing to GitHub.")
    args = parser.parse_args()

    config = load_config()

    try:
        clone_and_push_repositories(config, args.only_clone_repos)
    except Exception as e:
        logging.error(f"An error occurred: {e}")

if __name__ == "__main__":
    main()