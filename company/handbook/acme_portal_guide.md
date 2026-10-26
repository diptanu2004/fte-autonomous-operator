# Acme Portal — User Guide

The Acme Portal (`http://127.0.0.1:5055`) is the system of record for purchasing, accounts payable, HR and IT.

- **Sign in** at `/login`. Sessions expire after a few minutes of use; if you see the sign-in page again,
  simply sign in again and continue.
- **Purchase Orders** (`/purchase-orders`): paginated list (8 per page) with a search box. Search by PO number
  or vendor. Click a PO number for details (vendor, approved amount, invoices already booked against it).
- **Invoices** (`/invoices`): list of recorded invoices. **Record new invoice** at `/invoices/new`.
- **Vendors** (`/vendors`): vendor IDs and billing contact emails.
- **Employees** (`/employees`), **Add employee** at `/employees/new`.
- **IT Tickets** (`/tickets`), **Open new ticket** at `/tickets/new`.

Known behaviour: the invoice service occasionally returns *"temporarily unavailable (503)"*. Nothing is saved
in that case — re-open the form and submit again. Validation errors are shown in red at the top of the form;
read them, fix the field and resubmit.
