"""Message types exchanged over the ad-hoc radio and the processor mesh."""

BEACON = "beacon"            # processor -> collectors in range
UPLOAD = "upload"            # collector -> processors in range (and DTN relays)
RECEIVED = "received"        # processor -> collector: delivery certificate
RESULT = "result"            # processor -> collector: integration outcome
NOTICE = "notice"            # processor -> collector: account / task events
CLAIM = "claim"              # collector -> processor: claim a task
REAUTH = "reauth"            # collector -> processor: re-authentication request
REGISTER = "register"        # collector -> processor: field registration
ASK = "ask"                  # requester -> any worker node
MAP = "map"                  # worker node -> requester / processor -> collector
FREEZE = "freeze"            # freeze broadcast, amplified by every node
RELAY = "relay"              # collector -> collector: store-and-forward packet
TX = "tx"                    # processor mesh: transaction gossip
