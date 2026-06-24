app_name = "infinary_agent"
app_title = "Infinary Agent"
app_publisher = "Infinary"
app_description = "Infinary's outbound site agent: the version vector + dual drift fingerprint a managed ERPNext instance reports home for safe upgrades."
app_email = "ops@infinary.io"
app_license = "MIT"

# This app ships no doctypes, fixtures, or UI — only whitelisted read methods in
# infinary_agent.api that the outbound sidecar calls via `bench execute`. It is
# deliberately inert on the site: it observes, it does not modify.
