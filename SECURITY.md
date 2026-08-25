# Security policy

## Reporting

Report vulnerabilities through GitHub's private advisory form
(**Security → Report a vulnerability**) rather than a public issue.

## Threat model

This toolkit ingests data that hostile parties control. That is not incidental
to the project — it is the subject of it. Assume the following are adversarial:

- **AIS ship names, callsigns and destinations.** Free text broadcast over
  unauthenticated VHF. Anything that renders them must escape them. The HTML
  report escapes both its table cells and the JSON embedded in its `<script>`
  block; a name containing `</script>` is a realistic input, not a contrived one.
- **AIS positions, IMO and MMSI identifiers.** Routinely spoofed. Detecting
  that is the point, so parsers must survive nonsense rather than trust it.
- **Downloaded sanctions files.** Fetched over the network and parsed with
  regular expressions.

Reports involving any of these are in scope, as is anything that causes the
toolkit to silently drop or corrupt data — a detector that quietly reports
"no findings" is a more damaging failure here than one that crashes.

## Out of scope

The accuracy of zone coordinates and risk weights. Those are judgement calls
documented in the README; open a normal issue to argue with them.
