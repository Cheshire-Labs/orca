"""One owner per physical fact.

Every fact about the physical world -- where a labware is, what it holds, what
is mounted on a channel -- is answered here and nowhere else. A read that finds
nothing answers UNKNOWN; it never falls back to a template, a driver report or a
default, because each of those turns a guess into an answer that looks measured.

The module speaks identities and its own value types. It does not take or return
a Location, a holder, a device or a system map, so it cannot form an import cycle
with the topology and can be tested with none of it. Resolving an id to an object
is the caller's business.

Every physical fact has exactly one owner and no read-time fallback, and
only two things may write a position.
"""
