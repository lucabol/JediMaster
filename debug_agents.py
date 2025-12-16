"""Debug script to check agent listing."""
import os
from dotenv import load_dotenv
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

load_dotenv()
endpoint = os.getenv('AZURE_AI_FOUNDRY_PROJECT_ENDPOINT')

print(f"Endpoint: {endpoint}")
print(f"Creating client...")

client = AIProjectClient(
    endpoint=endpoint, 
    credential=DefaultAzureCredential(exclude_cli_credential=True)
)

print(f"Client created: {client}")
print(f"\nAgents client: {client.agents}")
print(f"Type: {type(client.agents)}")

print(f"\nCalling list_agents()...")
agents_pager = client.agents.list_agents()
print(f"Pager type: {type(agents_pager)}")

print(f"\nIterating over agents...")
agents_list = []
for agent in agents_pager:
    print(f"  Found agent: {agent.name} (ID: {agent.id})")
    agents_list.append(agent)

print(f"\nTotal agents found: {len(agents_list)}")

if len(agents_list) == 0:
    print("\n❌ No agents found. Possible reasons:")
    print("   1. Agents are in a different project")
    print("   2. Endpoint URL is incorrect")
    print("   3. Authentication doesn't have permission to list agents")
    print("   4. Agents are in a different hub/resource")
