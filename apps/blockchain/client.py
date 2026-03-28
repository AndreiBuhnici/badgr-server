from web3 import Web3
from django.conf import settings

w3 = Web3(Web3.HTTPProvider(settings.BLOCKCHAIN_RPC_URL))