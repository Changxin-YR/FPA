"""production domain: batches / feeding / harvests.

Module layout is isomorphic to master_data (DEVELOPMENT.md Sec 3 fixed seven steps):

    service.py        state machines + resource declarations + shared labels
    batches.py        read path  (list / detail / reload)
    feedings.py       read path  (list / detail / reload)
    harvests.py       read path  (list / detail / reload)
    write.py          write path (create / update / submit / verify / close)
    capabilities.py   capability declarations (12), deriving routes / permissions /
                      scope / idempotency / confirmation / invariants

The composition root `fpa.bootstrap` discovers `capabilities.py` by directory, so
adding this domain requires NO edit to any shared file -- that is the property
that lets the five domains proceed in parallel without colliding on one import
list.
"""
