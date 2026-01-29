"""
Deploy agent definitions from YAML files to Azure AI Foundry.

This script reads agent definitions from the foundry_agents/ folder
and creates or updates them in Azure AI Foundry.
"""

import os
import sys
import yaml
import argparse
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv


def load_agent_definitions(folder: str = "foundry_agents") -> list:
    """Load all agent definitions from YAML files in the specified folder."""
    definitions = []
    
    if not os.path.exists(folder):
        print(f"ERROR: Folder '{folder}' not found")
        return definitions
    
    for filename in os.listdir(folder):
        if filename.endswith('.yaml') or filename.endswith('.yml'):
            filepath = os.path.join(folder, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    agent_def = yaml.safe_load(f)
                    agent_def['_source_file'] = filename
                    definitions.append(agent_def)
            except Exception as e:
                print(f"WARNING: Failed to load {filename}: {e}")
    
    return definitions


def deploy_agent(client: AIProjectClient, agent_def: dict, dry_run: bool = False) -> bool:
    """Deploy a single agent definition to Azure Foundry."""
    name = agent_def.get('name')
    model = agent_def.get('model')
    instructions = agent_def.get('instructions')
    description = agent_def.get('description', '')
    
    if not name or not model or not instructions:
        print(f"  ERROR: Missing required fields (name, model, instructions)")
        return False
    
    print(f"\nDeploying: {name}")
    print(f"  Model: {model}")
    print(f"  Instructions length: {len(instructions)}")
    
    if dry_run:
        print(f"  [DRY RUN] Would create/update agent '{name}'")
        return True
    
    # Create the agent definition object
    definition = PromptAgentDefinition(
        model=model,
        instructions=instructions
    )
    
    try:
        # Check if agent already exists
        try:
            existing = client.agents.get(agent_name=name)
            print(f"  Agent exists (ID: {existing.id}), updating...")
            
            # Update existing agent
            agent = client.agents.update(
                agent_name=name,
                definition=definition,
                description=description if description else None
            )
            print(f"  ✓ Updated agent: {name}")
            
        except Exception as e:
            # Agent doesn't exist, create new
            if "not_found" in str(e).lower() or "not found" in str(e).lower() or "404" in str(e):
                print(f"  Agent does not exist, creating...")
                agent = client.agents.create(
                    name=name,
                    definition=definition,
                    description=description if description else None
                )
                print(f"  ✓ Created agent: {name}")
            else:
                raise e
        
        return True
        
    except Exception as e:
        print(f"  ✗ Failed to deploy: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description='Deploy agents to Azure AI Foundry')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be deployed without making changes')
    parser.add_argument('--folder', default='foundry_agents', help='Folder containing agent YAML files')
    parser.add_argument('--agent', help='Deploy only a specific agent by name')
    args = parser.parse_args()
    
    load_dotenv()
    
    endpoint = os.getenv('AZURE_AI_FOUNDRY_PROJECT_ENDPOINT')
    if not endpoint:
        print("ERROR: AZURE_AI_FOUNDRY_PROJECT_ENDPOINT not set in .env")
        sys.exit(1)
    
    print(f"Azure Foundry Endpoint: {endpoint}")
    print(f"Agent definitions folder: {args.folder}")
    
    if args.dry_run:
        print("\n*** DRY RUN MODE - No changes will be made ***\n")
    
    # Load agent definitions
    definitions = load_agent_definitions(args.folder)
    
    if not definitions:
        print("No agent definitions found.")
        sys.exit(1)
    
    print(f"\nFound {len(definitions)} agent definition(s):")
    for d in definitions:
        print(f"  - {d.get('name')} ({d.get('_source_file')})")
    
    # Filter by specific agent if requested
    if args.agent:
        definitions = [d for d in definitions if d.get('name') == args.agent]
        if not definitions:
            print(f"\nERROR: Agent '{args.agent}' not found in definitions")
            sys.exit(1)
    
    # Connect to Azure Foundry
    print("\nConnecting to Azure AI Foundry...")
    client = AIProjectClient(
        endpoint=endpoint,
        credential=DefaultAzureCredential()
    )
    
    # Deploy agents
    success_count = 0
    fail_count = 0
    
    for agent_def in definitions:
        if deploy_agent(client, agent_def, dry_run=args.dry_run):
            success_count += 1
        else:
            fail_count += 1
    
    # Summary
    print("\n" + "=" * 50)
    print("DEPLOYMENT SUMMARY")
    print("=" * 50)
    print(f"  Successful: {success_count}")
    print(f"  Failed: {fail_count}")
    
    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
