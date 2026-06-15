"""Well-known TCP port -> service ROLE (protocol), for naming port-detected services.

A listening port tells you the *role* (what protocol is being served), not the
*product*. So values here are deliberately protocol/role names only -- "HTTP",
"MySQL", "cPanel" -- and NEVER a specific product like "nginx" / "Apache" /
"LiteSpeed". Distinguishing the product requires reading the service's banner,
which the agent does on the host (see agent/stacksense_agent.py probes); that
becomes the Service.display_name. This map is the honest fallback when no banner
is available, and it gives already-collected `port-N` rows a meaningful label
without any agent change.

`role_for_port` returns None for unknown / ephemeral ports -- the display layer
uses that to push them into the collapsed "background" group rather than show
them as key services.
"""

# port -> role/protocol label (no product names by design)
PORT_ROLES = {
    # web / proxy  (role only -- product comes from the banner, never the port)
    80: "HTTP",
    443: "HTTPS",
    81: "HTTP-Alt",
    8080: "HTTP-Alt",
    8443: "HTTPS-Alt",
    # cPanel / WHM control planes
    2082: "cPanel",
    2083: "cPanel (SSL)",
    2086: "WHM",
    2087: "WHM (SSL)",
    2095: "Webmail",
    2096: "Webmail (SSL)",
    2077: "cpdavd",
    2078: "cpdavd (SSL)",
    2079: "cpdavd",
    2080: "cpdavd (SSL)",
    # mail
    25: "SMTP",
    465: "SMTP (SSL)",
    587: "SMTP (submission)",
    110: "POP3",
    995: "POP3 (SSL)",
    143: "IMAP",
    993: "IMAP (SSL)",
    4190: "Sieve",
    # dns
    53: "DNS",
    953: "DNS (rndc)",
    # ftp / ssh
    21: "FTP",
    22: "SSH",
    # databases / cache
    3306: "MySQL",
    5432: "PostgreSQL",
    6379: "Redis",
    27017: "MongoDB",
}


def role_for_port(port):
    """Return the role/protocol label for a well-known port, or None if unknown.

    None is the signal that a port is unrecognized/ephemeral and should not be
    surfaced as a key service.
    """
    try:
        return PORT_ROLES.get(int(port))
    except (TypeError, ValueError):
        return None
