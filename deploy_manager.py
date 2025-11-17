# deploy_manager.py

import os
import json
import argparse
import subprocess
import time
from typing import Dict, List, Any

STATE_FILE = "deployment_state.json"

# --- Serialization and State Helpers (Crucial for reading/writing the JSON state) ---

def read_state() -> Dict[str, Any]:
    """Reads deployment state from file, or initializes a new state."""
    if os.path.exists(STATE_FILE):
        print(f"Reading existing {STATE_FILE}.")
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    else:
        # Initial state (live_slot_index=null, next_deploy_slot_index=0)
        print(f"Initializing new {STATE_FILE} for first run.")
        return {
            "live_slot_index": None,
            "active_slots": {},
            "next_deploy_slot_index": 0,
            "version_to_port_map": {}
        }

def write_state(state: Dict[str, Any]):
    """Writes updated deployment state back to file."""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4) # Use indent for readability
    print(f"State written to {STATE_FILE}.")

# --- Core Deployment and Logic Functions ---

def run_shell(command: str):
    """Executes a shell command and prints output, raising an error on failure."""
    print(f"\n$ {command}")
    try:
        # Use subprocess.run to execute the command and capture/display output
        subprocess.run(command, shell=True, check=True, text=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Command failed with exit code {e.returncode}.")
        print(f"Stderr: {e.stderr}")
        raise # Re-raise to fail the Python script, which will fail the Jenkins stage

def get_ports(state: Dict[str, Any], port_pool: List[int]) -> tuple[int, int]:
    """Determines the current green deploy port and the current live port."""
    max_slots = len(port_pool)
    
    # 1. Determine next GREEN port
    next_deploy_index = int(state.get("next_deploy_slot_index", 0))
    current_deploy_port = port_pool[next_deploy_index % max_slots] # Ensure we stay in bounds
    
    # 2. Determine current LIVE port
    live_slot_index = state.get("live_slot_index")
    live_port = port_pool[int(live_slot_index)] if live_slot_index is not None else None
    
    print(f"Determined current deploy port (Green): {current_deploy_port}")
    print(f"Determined current LIVE port (Blue): {live_port if live_port is not None else 'N/A - First Deployment'}")
    
    return current_deploy_port, live_port

def deploy_new_version(args, current_deploy_port: int):
    """Handles Cleanup, Docker Prep, Build, and Health Check."""
    project_name = f"{args.project_name}-{current_deploy_port}"
    health_url = f"http://localhost:{current_deploy_port}/api/message" # Assuming the /api/message endpoint
    
    print(f"--- Deploying Version {args.version} on Port {current_deploy_port} (Project: {project_name}) ---")
    
    # 1. Cleanup Old Slot (Must be targeted, use the assigned port)
    print("Cleanup: Attempting to take down old services on this slot...")
    run_shell(f"docker compose -p {project_name} down --rmi all || true") # `|| true` to not fail if project doesn't exist

    # 2. Prepare Docker Compose (Modify the host port binding)
    print("Prep: Modifying docker-compose.yml to expose Nginx on the new host port.")
    # This sed command assumes 'docker-compose.yml' in the current directory
    # It finds lines like '  ports: ["OLD_PORT:80"]' and replaces 'OLD_PORT' with 'current_deploy_port'
    run_shell(f"sed -i 's/^\\(\\s*ports:\\s*\\-\\s*\\\"\\)[0-9]*:\\([0-9]*\\\"\\)/\\1{current_deploy_port}:\\2/g' docker-compose.yml")


    # 3. Build & Deploy (The core action)
    print(f"Build: Running docker compose up -d --build with unique project name...")
    run_shell(f"docker compose -p {project_name} up -d --build")
    
    # 4. Health Check (The quality gate)
    print(f"Health Check: Waiting 10s then checking status code for {health_url}...")
    time.sleep(10)
    
    curl_command = (
        f"STATUS=$(curl -o /dev/null -s -w '%{{http_code}}\\n' --max-time 15 {health_url} || echo '000'); "
        f"echo 'Response Status Code: $STATUS'; "
        f"if [ \"$STATUS\" != \"{args.expected_status}\" ]; then exit 1; fi;"
    )
    run_shell(curl_command) # If health check fails (exit code 1), run_shell will raise error
    
    print("Health Check Passed: New version is ready for traffic switch.")

def switch_and_update(args, current_deploy_port: int, live_port: int):
    """Updates the state file to mark the new version as LIVE."""
    state = read_state()
    port_pool = [int(p) for p in args.port_pool_str.split(',')]
    max_slots = len(port_pool)
    
    # Calculate new state indices
    new_live_slot_index = port_pool.index(current_deploy_port)
    new_next_deploy_slot_index = (new_live_slot_index + 1) % max_slots
    
    # Update state dictionary
    state["live_slot_index"] = new_live_slot_index
    state["next_deploy_slot_index"] = new_next_deploy_slot_index
    state["active_slots"][str(current_deploy_port)] = args.version # Store version on this port
    state["version_to_port_map"][args.version] = current_deploy_port # Map version to its port
    
    write_state(state)
    
    print(f"Traffic Switch complete (State Updated). New LIVE port: {current_deploy_port}, next deploy slot: {port_pool[new_next_deploy_slot_index]}.")


def rollback_on_failure(args, failed_deploy_port: int):
    """
    Handles the rollback procedure when a *new* deployment fails its health check.
    This means the old LIVE version is still active and the state file hasn't been updated.
    """
    state = read_state()
    port_pool = [int(p) for p in args.port_pool_str.split(',')]
    
    print(f"--- Initiating Rollback for failed deployment on port {failed_deploy_port} ---")

    # If the new deployment failed its health check, the 'live_slot_index' in the state file
    # still points to the PREVIOUSLY ACTIVE version. We just need to clean up the failed one.
    
    old_live_index = state.get("live_slot_index")
    if old_live_index is not None:
        old_live_port = port_pool[int(old_live_index)]
        old_live_version = state["active_slots"].get(str(old_live_port))
        print(f"Previous LIVE version {old_live_version} on port {old_live_port} is still active. No state change needed for rollback.")
    else:
        # This occurs on the very first deployment if it fails. No rollback target.
        print("This was the first deployment attempt and it failed. No previous version to roll back to.")
        
    # Always clean up the failed deploy project.
    print(f"Cleanup: Removing failed deployment project '{args.project_name}-{failed_deploy_port}'...")
    run_shell(f"docker compose -p {args.project_name}-{failed_deploy_port} down --rmi all || true")
    print("Rollback/Cleanup complete. Previous state is preserved.")


# --- Main Execution Block ---

def main():
    parser = argparse.ArgumentParser(description="Unified N-Green Deployment Manager (Python).")
    parser.add_argument("--action", required=True, help="Action to perform ('deploy').")
    parser.add_argument("--version", required=True, help="Version string for the deployment (e.g., v1.0.0).")
    parser.add_argument("--expected-status", required=True, type=str, help="Expected HTTP status code for health checks (e.g., '200').")
    parser.add_argument("--rollback-target", default="", help="Specific version to rollback to. Leave empty for instant N-Green to previous live.")
    parser.add_argument("--project-name", required=True, help="Base name for Docker Compose projects (e.g., 'nginx_workflow').")
    parser.add_argument("--port-pool-str", required=True, help="Comma-separated string of ports available for deployment (e.g., '5000,5001,5002,5003').")
    
    args = parser.parse_args()
    
    try:
        if args.action == "deploy":
            port_pool = [int(p) for p in args.port_pool_str.split(',')]
            state = read_state()
            current_deploy_port, live_port = get_ports(state, port_pool)
            
            # 1. Attempt Deployment and Health Check
            deploy_new_version(args, current_deploy_port)
            
            # 2. If successful, switch traffic (update state file)
            switch_and_update(args, current_deploy_port, live_port)
            
            print("\nDeployment SUCCESSFUL and state updated.")

        # This part will be expanded for other actions like 'rollback' for specific scenarios,
        # but for deploy failures, the rollback logic is tied to the exception handling.
        elif args.action == "rollback_force": # Example for a separate rollback action (not for deploy failure)
            # This would be a user-initiated rollback outside of a deploy failure
            pass

    except Exception as e:
        print(f"\n--- FATAL DEPLOYMENT FAILURE ---")
        print(f"Reason: {e}")
        
        # On failure during deploy_new_version, attempt to rollback/cleanup the failed slot
        try:
            port_pool = [int(p) for p in args.port_pool_str.split(',')]
            state = read_state()
            current_deploy_port, _ = get_ports(state, port_pool) # Get the port that was being used for the failed deploy
            rollback_on_failure(args, current_deploy_port)
            
        except Exception as rollback_e:
            print(f"\n--- FATAL ROLLBACK/CLEANUP FAILURE ---")
            print(f"Even cleanup failed completely: {rollback_e}")
            
        # Re-raise the original error to ensure the Jenkins stage fails.
        raise

if __name__ == "__main__":
    main()
