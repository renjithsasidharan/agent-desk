from .base import Broker, BrokerError, Contract, OrderResult, round_tick


def get_broker(cfg, secrets, state_dir) -> Broker:
    name = cfg["broker"]
    if name == "groww":
        from .groww import GrowwBroker
        real = GrowwBroker(secrets, state_dir)
    else:
        raise BrokerError(f"no adapter for broker '{name}'")
    if cfg["mode"] == "paper":
        from .paper import PaperBroker
        return PaperBroker(real, state_dir, cfg["paper"]["slippage_pct"])
    if cfg["mode"] != "live":
        raise BrokerError(f"mode must be paper or live, not {cfg['mode']}")
    return real
