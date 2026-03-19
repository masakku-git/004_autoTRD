import logging
import logging.config
import pathlib

import yaml


def setup_logging() -> logging.Logger:
    config_path = pathlib.Path(__file__).parent.parent.parent / "config" / "logging.yaml"
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)
        # Ensure log directory exists
        log_dir = pathlib.Path(__file__).parent.parent.parent / "data"
        log_dir.mkdir(exist_ok=True)
        logging.config.dictConfig(config)
    else:
        logging.basicConfig(level=logging.INFO)
    return logging.getLogger("autotrd")


logger = setup_logging()
