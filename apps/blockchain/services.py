from .contract import contract, w3

account = w3.eth.accounts[0]

def register_credential(credential_hash, issuerId):

    tx_hash = contract.functions.registerCredential(credential_hash, issuerId).transact({
        "from": account
    })

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    return receipt

def get_credential(credential_hash):
    return contract.functions.getCredential(credential_hash).call()