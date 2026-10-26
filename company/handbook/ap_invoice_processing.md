# SOP: Accounts Payable — Vendor Invoice Processing

Owner: Finance (Maria Gomez, Finance Manager) · Applies to: all vendor invoices received in the shared inbox.

## When to use
Any request to "process", "book", "enter" or "clear" vendor invoices. Invoices arrive as files in the
`inbox/` folder of the shared workspace.

## Procedure (for EACH invoice file in `inbox/`)
1. Read the invoice file. Extract: invoice number, vendor name, PO number, invoice date, total amount.
   The **invoice total is the sum of ALL line items** (including surcharges, fees and taxes). If no total is
   printed, add up every line with `calculate` — never take a single line as the total.
2. Check the invoice is not already recorded: open **Invoices** in the Acme Portal. If the invoice number is
   already listed, skip it and note "duplicate" in the summary.
3. Open the referenced purchase order in the portal (**Purchase Orders** → search the PO number → open it).
   Confirm the PO belongs to the same vendor and note the approved PO amount.
4. **Match the amounts.** The invoice matches if it is within **2%** of the PO approved amount.
5. Record the invoice in the portal (**Invoices → Record new invoice**):
   - Portal dates must be `YYYY-MM-DD`; amounts must be plain numbers (e.g. `7850.00`, no `$` or commas).
   - Select the vendor whose ID matches the PO.
   - If amounts match → status **Approved for payment**, notes "Matched PO <PO number>".
   - If amounts do not match → status **On hold**, notes "Amount mismatch: invoice <amt> vs PO <amt>".
6. **Approval limit:** an invoice of **more than USD 5,000** may only be saved as *Approved for payment* after
   sign-off by the Finance Manager. The operator must obtain approval before saving (the system will enforce
   this and prompt a human).
7. For every invoice put **On hold**, email the vendor's billing contact (email listed on the **Vendors** page)
   using the "Invoice on hold" template in the communication guidelines.

## Close-out
Write a short summary report to `reports/ap_summary.md` listing each invoice: number, vendor, PO, amount,
outcome (approved / on hold / duplicate) and any email sent.

## Done means
- Every invoice in `inbox/` appears on the portal **Invoices** page with the correct status.
- On-hold vendors have been emailed.
- `reports/ap_summary.md` exists and covers every invoice.
