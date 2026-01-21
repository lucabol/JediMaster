"""
Extract agent definitions from Azure AI Foundry and save them as YAML files.
"""

import os
import yaml
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

def extract_agents():
    """Extract all agents from Azure Foundry and save as YAML."""
    load_dotenv()
    
    endpoint = os.getenv('AZURE_AI_FOUNDRY_PROJECT_ENDPOINT')
    if not endpoint:
        print("ERROR: AZURE_AI_FOUNDRY_PROJECT_ENDPOINT not set in .env")
        return
    
    print(f"Connecting to: {endpoint}")
    
    client = AIProjectClient(
        endpoint=endpoint,
        credential=DefaultAzureCredential(exclude_cli_credential=True)
    )
    
    # Create output directory
    output_dir = "foundry_agents"
    os.makedirs(output_dir, exist_ok=True)
    
    # List all agents
    agents = list(client.agents.list())
    
    print("\n=== Extracting Agents ===\n")
    
    for agent in agents:
        # Access internal data structure
        agent_data = agent._data
        versions = agent_data.get('versions', {})
        latest = versions.get('latest', {})
        definition = latest.get('definition', {})
        metadata = latest.get('metadata', {})
        
        name = agent.name
        model = definition.get('model', 'unknown')
        instructions = definition.get('instructions', '')
        description = metadata.get('description', '')
        
        print(f"Agent: {name}")
        print(f"  ID: {agent.id}")
        print(f"  Model: {model}")
        print(f"  Instructions length: {len(instructions)}")
        
        # Build agent definition
        agent_def = {
            'name': name,
            'model': model,
            'instructions': instructions,
        }
        
        if description:
            agent_def['description'] = description
        
        # Add tools if present
        tools = definition.get('tools', [])
        if tools:
            agent_def['tools'] = tools
        
        # Save to YAML
        filename = f"{name.lower().replace(' ', '_')}.yaml"
        filepath = os.path.join(output_dir, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(agent_def, f, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120)
        
        print(f"  Saved to: {filepath}")
        print()
    
    print(f"Done! Agent definitions saved to {output_dir}/")

if __name__ == "__main__":
    extract_agents()
