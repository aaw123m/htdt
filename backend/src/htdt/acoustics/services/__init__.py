"""Acoustics services layer (#954): orchestration
modules that drive the domain authorities — solver adapters (pffdtd,
geometric, hybrid), the candidate wave execution engine, the stochastic
ray receiver, object promotion, treatment services, and the solver
output ledger. May import domain + persistence (+ sibling services);
never ui.
"""
