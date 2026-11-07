# Spec Delta

## Purpose

Find the live web services among hosts and record everything httpx can say about each one, from the `webs` module through `temporal_web_probe`.

## ADDED Requirements

### Requirement: Web probe tool
`temporal_web_probe` SHALL accept either targets (see `module-runs`) or `from_run`, a completed `subdomains` run whose `subdomains.txt` supplies the hosts.

It SHALL also accept:
- an optional list of ports, 1 to 65535, at most 100;
- an optional fleet id;
- a `chunk_size`, defaulting to 200.

It SHALL start a run of the `webs` module and return its run id at once.

#### Scenario: Probe an enumeration's output
- **WHEN** an agent calls `temporal_web_probe(from_run=<completed subdomains run>)`
- **THEN** a `webs` run starts over that run's resolved subdomains

#### Scenario: Custom ports
- **WHEN** an agent calls `temporal_web_probe(targets=["example.com"], ports=[443, 8443])`
- **THEN** httpx probes `example.com` on ports 443 and 8443 only

### Requirement: Verbose probe fields
For every live site, the probe SHALL record what httpx reports for:
- the URL, input, scheme, host and port;
- the status code, content length and content type;
- the title, web server and detected technologies;
- the CNAMEs, IPs and CDN;
- the redirect location;
- the favicon hash and body hash;
- the response time, word and line counts;
- the TLS certificate data.

Redirects SHALL be followed within the same host.

#### Scenario: Site behind a CDN
- **WHEN** a probed host is a CNAME to a CDN and answers 200
- **THEN** its JSON line carries the status code, content length, technologies, CNAME chain, IPs and CDN name

### Requirement: Web probe output
Each run SHALL produce:
- `webs.jsonl`: one httpx JSON object per live site;
- `webs.txt`: one human-readable line per site, `<url> [<status>] [<length>] [<content type>] [<title>] [<server>] [<technologies>] [<cname>] [<ips>] [<cdn>]`;
- `urls.txt`: the live URLs, one per line.

Its result SHALL report the number of targets, live sites and chunks.

#### Scenario: Readable output
- **WHEN** a probe finds `https://app.example.com` answering 200 with nginx and React
- **THEN** `webs.txt` has a line starting `https://app.example.com [200]` that names nginx and React, and `urls.txt` lists the URL

### Requirement: Probe pacing
httpx SHALL use at most 150 requests per second per worker, a 10-second timeout and 2 retries. A chunk SHALL time out after 60 minutes.

#### Scenario: Large chunk
- **WHEN** a chunk of 200 hosts is probed
- **THEN** the worker never exceeds 150 requests per second
