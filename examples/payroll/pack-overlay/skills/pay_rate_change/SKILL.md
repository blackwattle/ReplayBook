---
name: pay-rate-change
description: Change an employee's hourly pay rate as approved in the ticket.
request_type: pay_rate_change
tools: payroll_get_employee, payroll_set_pay_rate, ticket_ask_requester, ticket_comment
---

# Pay rate change

## When this applies
A manager or HR asks for an employee's hourly rate to change, with the employee id
(like `E1001`), the new rate and usually an effective date.

## Information you need
- employee id
- the new hourly rate
- effective date (optional)

If the employee id or the new rate is missing, ask the requester with
`ticket_ask_requester` and stop. Never estimate a rate.

## Procedure
1. `payroll_get_employee` to confirm the employee exists and see the current rate.
2. `payroll_set_pay_rate` with the employee id, the new hourly rate and the effective date
   when one is given.
3. `ticket_comment` with a one-line summary.

## Never
- Never call `payroll_delete_employee`.
- Never add allowances in a pay rate ticket.
