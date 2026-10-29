from .base import Broker, BrokerError, Contract, OrderResult, round_tick


_real = {}  # one live connection per broker, shared by all books in a process


def get_broker(cfg, secrets, state_dir, book_dir=None) -> Broker:
    """state_dir holds the shared login token and caches; book_dir the book's paper orders."""
    name = cfg["broker"]
    if name not in _real:
        if name == "groww":
            from .groww import GrowwBroker
            _real[name] = GrowwBroker(secrets, state_dir)
        else:
            raise BrokerError(f"no adapter for broker '{name}'")
    real = _real[name]
    if cfg["mode"] == "paper":
        from .paper import PaperBroker
        return PaperBroker(real, book_dir or state_dir, cfg["paper"]["slippage_pct"])
    if cfg["mode"] != "live":
        raise BrokerError(f"mode must be paper or live, not {cfg['mode']}")
    return real
