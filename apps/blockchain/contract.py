import json

from apps import badgrlog
from apps.mainsite import TOP_DIR
from .client import w3

logger = badgrlog.BadgrLogger()

with open(TOP_DIR + "/truffle/build/contracts/CredentialsRegistry.json") as f:
    contract_json = json.load(f)

network_id = w3.net.version

if network_id not in contract_json["networks"].keys():
    raise Exception(f"Contract not deployed to network with id {network_id}")

abi = contract_json["abi"]
address = contract_json["networks"][network_id]["address"]

contract = w3.eth.contract(
    address=address,
    abi=abi
)

# This should not be possible on a normal network, but since
# for testing we can "destroy" the network, this helps track bugs
code = w3.eth.get_code(contract.address)
if code == b'':
    raise Exception(f"Contract not found")