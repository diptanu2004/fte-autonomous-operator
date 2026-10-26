# Communication Guidelines

All outbound email is sent from **ops@acme-corp.com** using the operator's `send_email` tool.
Internal addresses end with `@acme-corp.com`. Anything else is external.

- Internal emails may be sent freely.
- **External emails (vendors, customers) require human approval before sending.** The system enforces this.
- Be brief, factual and polite. Never include internal approval limits or other vendors' data.

## Template: Invoice on hold (to vendor)
Subject: `Invoice <invoice number> on hold — amount does not match PO <PO number>`

Body:
> Hello <vendor name> team,
>
> We received invoice <invoice number> for USD <invoice amount>. The related purchase order <PO number> was
> approved for USD <PO amount>, so we have placed the invoice on hold. Please send a corrected invoice or a
> credit note for the difference, or reply with supporting documentation.
>
> Regards,
> Acme Corp Accounts Payable

## Template: New hire welcome (to manager)
Subject: `Onboarding started: <full name> (<start date>)`

Body: who is joining, start date, employee ID, IT ticket numbers raised, and that pre-start credentials go to
their personal email.
