![CI](https://github.com/shepdogg6t7-glitch/azure-cost-watchdog/actions/workflows/ci.yml/badge.svg)

# azure-cost-watchdog

**Find idle and orphaned Azure resources, price the waste per resource, and stop it — behind guardrails strict enough that auto-remediation is safe to actually switch on.**

Most "cost saving" scripts are a cron job with a delete permission attached. This one is built the other way round: every action has to get past a named guardrail, every decision carries its own evidence, and the default mode changes nothing at all. You can read one decision in the report and see exactly which numbers and which rule produced it.

monthly run rate USD 886.80
identified waste USD 512.78
actionable now USD 183.18
pending approval USD 189.34
resources considered 14 (4 healthy, 1 below threshold)


Identified waste is every decision the detectors priced. Of that, USD 183.18 is executable now, USD 189.34 waits on an approval (`environment=prod`), and USD 140.26 is held back by guardrails and will not be touched: two idle VMs, one carrying the `costwatchdog:protected` tag and one in a denylisted scope.

---

## Architecture

```mermaid
flowchart LR
  subgraph AZ["Audited Azure subscription"]
    RES["Resources<br/>VMs · VMSS · AKS · App Service plans<br/>disks · snapshots · public IPs · NICs"]
    CM["Cost Management API<br/>daily line items"]
    MON["Azure Monitor<br/>metrics + activity log"]
    RM["Resource Manager<br/>inventory + tags"]
    RES -.-> RM
  end

  subgraph WD["costwatchdog"]
    direction TB
    BILL["billing.py<br/>normalise to a monthly run rate"]
    DET["detectors.py<br/>idle · orphan · healthy"]
    POL["policy.json<br/>guardrails"]
    ENG["engine.py<br/>assess → guardrail → rank → cap"]
    REP["report.py<br/>Markdown · JSON · console"]
    LOG[("audit trail<br/>JSONL")]
    BILL --> ENG
    DET --> ENG
    POL --> ENG
    ENG --> REP
    ENG --> LOG
  end

  ID["user-assigned<br/>managed identity"] -.-> CM
  ID -.-> MON
  ID -.-> RM
  STORE[("Blob storage<br/>reports + trail")]
  CI["GitHub Actions<br/>audit + --fail-on-waste gate"]

  CM --> BILL
  MON --> DET
  RM --> BILL
  RM --> DET
  REP --> STORE
  LOG --> STORE
  ENG -. "apply (opt-in, dry-run by default)" .-> RES
  CI --> REP

