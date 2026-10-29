"""DEIMOSBC: a blockchain-based system for crowdsensing after natural disasters.

Reference implementation of Sarbajna, Eick & Laszka, BRAINS 2021
(https://ieeexplore.ieee.org/document/9569794).

Package layout (paper section in brackets):

* :mod:`deimosbc.ledger`     - blocks, transactions, the permissioned chain and
                               majority-attestation consensus [IV-A, IV-C]
* :mod:`deimosbc.contracts`  - the smart contracts: registry, tasks, map
                               submission, governance (release / freeze) [IV-C]
* :mod:`deimosbc.mapping`    - Data Summarization System: local and global map
                               graphs, overlap chaining, conflicts [IV-B.1]
* :mod:`deimosbc.tms`        - Trace Management System: displacement-based
                               sampling and trajectory segmentation [IV-B.2]
* :mod:`deimosbc.nodes`      - Data Collector, Data Processor, Requester [IV-A]
* :mod:`deimosbc.network`    - short-range ad-hoc radio model [III-A]
* :mod:`deimosbc.sim`        - hurricane scenario, ground truth, metrics [II]
"""

__version__ = "0.1.0"
