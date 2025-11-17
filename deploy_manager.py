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
        result = subprocess.run(command, shell=True, check=True, text=True, capture_output=True)
        print(result.stdout)
        if result.stderr:
            print(f"Stderr: {result.stderr}")
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Command failed with exit code {e.returncode}.")
        print(f"Stderr: {e.stderr}")
        raise # Re-raise to fail the Python script, which will fail the Jenkins stage

def get_ports(state: Dict[str, Any], port_pool: List[int]) -> tuple[int, int]:
    """Determines the current green deploy port and the current live port."""
    max_slots = len(port_pool)
    
    # 1. Determine next GREEN port (for the new deployment)
    next_deploy_index = int(state.get("next_deploy_slot_index", 0))
    current_deploy_port = port_pool[next_deploy_index % max_slots] # Ensure we stay in bounds
    
    # 2. Determine current LIVE port (the one currently serving traffic)
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
    # Using 'docker compose down' with a project name is idempotent, but we add || true to be safe
    run_shell(f"docker compose -p {project_name} down --rmi all --volumes --remove-orphans || true") 

    # 2. Prepare Docker Compose (Modify the host port binding in the docker-compose.yml file)
    print(f"Prep: Modifying docker-compose.yml to expose Nginx on host port {current_deploy_port}.")
    # This sed command assumes 'docker-compose.yml' in the current directory
    # It finds lines like '  - "OLD_PORT:80"' and replaces 'OLD_PORT' with 'current_deploy_port'
    # This is critical for dynamically assigning the host port for Nginx.
    run_shell(f"sed -i 's/^\\(\\s*\\-\\s*\\\"\\)[0-9]*:\\([0-9]*\\\"\\)/\\1{current_deploy_port}:\\2/g' docker-compose.yml")

    # 3. Build & Deploy (The core action)
    print(f"Build: Running docker compose up -d --build with unique project name...")
    run_shell(f"docker compose -p {project_name} up -d --build")
    
    # 4. Health Check (The quality gate)
    print(f"Health Check: Waiting 10s then checking status code for {health_url}...")
    time.sleep(10) # Give containers time to start
    
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
    state = read_state() # Read current state, which should still point to the old live
    port_pool = [int(p) for p in args.port_pool_str.split(',')]
    
    print(f"--- Initiating Rollback for failed deployment on port {failed_deploy_port} ---")

    old_live_index = state.get("live_slot_index")
    if old_live_index is not None:
        old_live_port = port_pool[int(old_live_index)]
        old_live_version = state["active_slots"].get(str(old_live_port), "UNKNOWN")
        print(f"Previous LIVE version '{old_live_version}' on port {old_live_port} is still active. No state change needed for rollback.")
    else:
        print("This was the first deployment attempt and it failed. No previous version to roll back to.")
        
    # Always clean up the failed deploy project.
    print(f"Cleanup: Removing failed deployment project '{args.project_name}-{failed_deploy_port}'...")
    # Use --volumes and --remove-orphans for thorough cleanup
    run_shell(f"docker compose -p {args.project_name}-{failed_deploy_port} down --rmi all --volumes --remove-orphans || true")
    print("Rollback/Cleanup complete. Previous state is preserved.")


# --- Main Execution Block ---

def main():
    parser = argparse.ArgumentParser(description="Unified N-Green Deployment Manager (Python).")
    parser.add_argument("--action", required=True, help="Action to perform ('deploy', 'rollback').")
    parser.add_argument("--version", required=False, help="Version string for the deployment/rollback (e.g., v1.0.0). Required for 'deploy'.")
    parser.add_argument("--expected-status", required=False, type=str, help="Expected HTTP status code for health checks (e.g., '200'). Required for 'deploy'.")
    parser.add_argument("--rollback-target-version", default="", help="Specific version to rollback to. Used with action 'rollback'. Leave empty for instant N-Green to previous live.")
    parser.add_argument("--project-name", required=True, help="Base name for Docker Compose projects (e.g., 'nginx_workflow').")
    parser.add_argument("--port-pool-str", required=True, help="Comma-separated string of ports available for deployment (e.g., '5000,5001,5002,5003').")
    
    args = parser.parse_args()
    
    # Validate arguments based on action
    if args.action == "deploy":
        if not args.version or not args.expected_status:
            parser.error("--version and --expected-status are required for 'deploy' action.")
    elif args.action == "rollback" and not args.rollback_target_version:
        # For 'rollback', if no target version is given, it means 'rollback to previous live'
        pass 
    elif args.action == "rollback" and args.rollback_target_version:
        # For 'rollback' with a target, nothing extra needed here.
        pass
    else:
        parser.error(f"Invalid action: {args.action}")


    try:
        port_pool = [int(p) for p in args.port_pool_str.split(',')]
        state = read_state()

        if args.action == "deploy":
            current_deploy_port, live_port = get_ports(state, port_pool)
            
            # 1. Attempt Deployment and Health Check
            deploy_new_version(args, current_deploy_port)
            
            # 2. If successful, switch traffic (update state file)
            switch_and_update(args, current_deploy_port, live_port)
            
            print("\nDeployment SUCCESSFUL and state updated.")

        elif args.action == "rollback":
            print(f"--- Initiating Manual Rollback ---")
            
            target_version = args.rollback_target_version

            if target_version:
                # Rollback to a SPECIFIC version
                if target_version not in state["version_to_port_map"]:
                    raise ValueError(f"Rollback target version '{target_version}' not found in deployment history.")
                
                target_port = state["version_to_port_map"][target_version]
                target_slot_index = port_pool.index(target_port)
                
                print(f"Rolling back to version '{target_version}' on port {target_port} (slot index {target_slot_index}).")
                
                # Perform a health check on the target port BEFORE switching.
                # If the target is not healthy, we cannot rollback to it.
                health_url = f"http://localhost:{target_port}/api/message"
                print(f"Performing health check on rollback target: {health_url}")
                curl_command = (
                    f"STATUS=$(curl -o /dev/null -s -w '%{{http_code}}\\n' --max-time 15 {health_url} || echo '000'); "
                    f"echo 'Response Status Code: $STATUS'; "
                    f"if [ \"$STATUS\" != \"200\" ]; then exit 1; fi;" # Assume 200 for rollback health check
                )
                run_shell(curl_command) # If health check fails, script fails

                # If health check passes, update state to make the target version live
                state["live_slot_index"] = target_slot_index
                state["next_deploy_slot_index"] = (target_slot_index + 1) % len(port_pool)
                
                write_state(state)
                print(f"Manual rollback to version '{target_version}' on port {target_port} SUCCESSFUL.")
                
            else:
                # Rollback to INSTANT PREVIOUS LIVE version
                current_live_index = state.get("live_slot_index")
                if current_live_index is None:
                    raise ValueError("Cannot perform rollback: No active live deployment found.")

                previous_live_slot_index = (current_live_index - 1 + len(port_pool)) % len(port_pool)
                previous_live_port = port_pool[previous_live_slot_index]
                previous_live_version = state["active_slots"].get(str(previous_live_port))

                if not previous_live_version:
                    raise ValueError(f"Cannot find a previous active version to rollback to in slot {previous_live_slot_index}.")
                
                print(f"Rolling back to instant previous live: Version '{previous_live_version}' on port {previous_live_port}.")
                
                # Perform health check on the target port BEFORE switching.
                health_url = f"http://localhost:{previous_live_port}/api/message"
                print(f"Performing health check on rollback target: {health_url}")
                curl_command = (
                    f"STATUS=$(curl -o /dev/null -s -w '%{{http_code}}\\n' --max-time 15 {health_url} || echo '000'); "
                    f"echo 'Response Status Code: $STATUS'; "
                    f"if [ \"$STATUS\" != \"200\" ]; then exit 1; fi;"
                )
                run_shell(curl_command) # If health check fails, script fails

                # If health check passes, update state
                state["live_slot_index"] = previous_live_slot_index
                state["next_deploy_slot_index"] = (previous_live_slot_index + 1) % len(port_pool)

                write_state(state)
                print(f"Instant rollback to previous live version '{previous_live_version}' SUCCESSFUL.")
                

    except Exception as e:
        print(f"\n--- FATAL DEPLOYMENT/ROLLBACK FAILURE ---")
        print(f"Reason: {e}")
        
        if args.action == "deploy":
            # On failure during deploy_new_version, attempt to cleanup the failed slot
            try:
                # If the script failed *before* switch_and_update, current_deploy_port is what we were trying to deploy
                # If it failed *after* switch_and_update (e.g., during some post-deploy step), it's more complex.
                # For a failed 'deploy' action, assume the 'current_deploy_port' was the one that failed.
                # We can safely get this from the state *before* it was modified or from the args.
                
                # Re-reading state to get the (potentially old) state and determine the port we were attempting to use
                _, failed_deploy_port_for_cleanup = get_ports(read_state(), port_pool)
                rollback_on_failure(args, failed_deploy_port_for_cleanup) 
                
            except Exception as cleanup_e:
                print(f"\n--- FATAL CLEANUP AFTER DEPLOY FAILURE ---")
                print(f"Even cleanup failed completely: {cleanup_e}")
        
        # Re-raise the original error to ensure the Jenkins stage fails.
        raise

if __name__ == "__main__":
    main()
