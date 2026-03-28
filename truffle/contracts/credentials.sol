// SPDX-License-Identifier: MIT 
pragma solidity ^0.8.0;

contract CredentialsRegistry {

    enum Status {
        active, inactive
    }

    struct CredentialRecord {
        address uploader;
        string issuerId;
        string credentialId;
        Status status;
        uint256 createdAt;
        uint256 updatedAt;
    }

    mapping(bytes32 => CredentialRecord) public credentials;

    event CredentialRegistered(
        bytes32 credentialHash,
        string issuerId,
        string credentialId,
        address uploader
    );

    function registerCredential(bytes32 credentialHash, string calldata credentialId, string calldata issuerId) public {
        require(credentials[credentialHash].createdAt == 0, "Already exists");
        require(credentialHash != bytes32(0), "Invalid hash");
        require(bytes(issuerId).length > 0, "Empty issuerId");
        require(bytes(credentialId).length > 0, "Empty credentialId");

        // Eventually for a real implementation only certain addresses should be able to upload

        credentials[credentialHash] = CredentialRecord(msg.sender, issuerId, credentialId, Status.active, block.timestamp, block.timestamp);

        emit CredentialRegistered(credentialHash, issuerId, credentialId, msg.sender);
    }

    function getCredential(bytes32 credentialHash) public view returns (bool exists, address uploader, string memory issuerId, string memory credentialId, string memory status, uint256 createdAt, uint256 updatedAt) {
        CredentialRecord memory record = credentials[credentialHash];
        string memory statusString = record.status == Status.active ? "active" : "inactive";

        if (record.createdAt == 0) {
            return (false, address(0), "", "", "inactive", 0, 0);
        }

        return (true, record.uploader, record.issuerId, record.credentialId, statusString, record.createdAt, record.updatedAt);
    }
}