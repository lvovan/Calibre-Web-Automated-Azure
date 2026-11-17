// Custom hostname with a free App Service managed certificate.
// Prerequisite DNS records (see docs/azure-deployment.md):
//   CNAME <customDomainName> -> <app>.azurewebsites.net
//   TXT   asuid.<customDomainName> -> customDomainVerificationId output

@description('Azure region.')
param location string

@description('Existing web app name.')
param webAppName string

@description('App Service plan resource ID (managed certificates are created on the plan).')
param planId string

@description('Fully qualified custom hostname, e.g. books.example.com.')
param customDomainName string

param tags object = {}

resource site 'Microsoft.Web/sites@2023-12-01' existing = {
  name: webAppName
}

// Phase 1: bind the hostname without TLS so the managed certificate can be validated
resource hostNameBinding 'Microsoft.Web/sites/hostNameBindings@2023-12-01' = {
  parent: site
  name: customDomainName
  properties: {
    siteName: webAppName
    hostNameType: 'Verified'
    sslState: 'Disabled'
  }
}

resource certificate 'Microsoft.Web/certificates@2023-12-01' = {
  // Same name as the portal uses, so a certificate created there is reused instead of conflicting
  name: '${customDomainName}-${webAppName}'
  location: location
  tags: tags
  properties: {
    serverFarmId: planId
    canonicalName: customDomainName
  }
  dependsOn: [
    hostNameBinding
  ]
}

// Phase 2: enable SNI TLS with the managed certificate (separate module: same resource, new state)
module sniBinding 'sniBinding.bicep' = {
  name: 'sni-${uniqueString(customDomainName)}'
  params: {
    webAppName: webAppName
    customDomainName: customDomainName
    thumbprint: certificate.properties.thumbprint
  }
}
