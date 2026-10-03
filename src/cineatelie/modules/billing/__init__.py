"""`billing` module (backend spec §4.3, milestone M2): plan state, plans, quotas and invoices.

Renewal and payment are not built yet. A subscription changes only when its row changes, which is
how every tier is exercised in every environment (backend spec US-BE-02).
"""
