const CredentialsRegistry = artifacts.require("CredentialsRegistry");

module.exports = function (deployer) {
  deployer.deploy(CredentialsRegistry);
};