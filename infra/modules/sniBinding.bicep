// Updates an existing hostname binding to SNI TLS with the given certificate thumbprint.

param webAppName string
param customDomainName string
param thumbprint string

resource site 'Microsoft.Web/sites@2023-12-01' existing = {
  name: webAppName
}

resource binding 'Microsoft.Web/sites/hostNameBindings@2023-12-01' = {
  parent: site
  name: customDomainName
  properties: {
    siteName: webAppName
    hostNameType: 'Verified'
    sslState: 'SniEnabled'
    thumbprint: thumbprint
  }
}
