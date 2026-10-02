#!/usr/bin/env python3
"""RSSgate entry point:  python run.py [--config config.yaml]"""
import argparse
import logging
import os

from rssgate import db
from rssgate.config import load_config
from rssgate.llm import LLMClient
from rssgate.scheduler import Scheduler
from rssgate.web import create_app


def main():
    ap = argparse.ArgumentParser(description="RSSgate feed manager")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if not os.path.exists(args.config):
        from rssgate.config import save_config
        save_config(load_config(args.config), args.config)
        print(f"created default config at {args.config}")

    cfg = load_config(args.config)
    os.makedirs(cfg["server"]["data_dir"], exist_ok=True)
    db_path = os.path.join(cfg["server"]["data_dir"], "rssgate.sqlite")
    conn = db.connect(db_path)
    db.init_db(conn)
    app = create_app(args.config, conn=conn)
    sched = Scheduler(conn, cfg, lambda: LLMClient(load_config(args.config)),
                      db_path=db_path)
    if cfg["polling"]["fetch_on_start"]:
        from rssgate.refresh import refresh_all
        import threading

        def _startup_poll():
            import contextlib
            with contextlib.closing(db.connect(db_path)) as pconn:
                refresh_all(pconn, cfg, LLMClient(cfg))
        threading.Thread(target=_startup_poll, daemon=True).start()
    sched.start()

    import threading as _th

    def _image_backfill():
        import contextlib
        import time
        time.sleep(15)   # let startup poll/digesters finish first
        with contextlib.closing(db.connect(db_path)) as bconn:
            from rssgate.refresh import backfill_images
            backfill_images(bconn, cfg)
    _th.Thread(target=_image_backfill, daemon=True).start()

    def _maintenance():
        import contextlib
        from rssgate import maint
        import time
        time.sleep(120)          # after startup poll + image backfill
        with contextlib.closing(db.connect(db_path)) as mconn:
            maint.run_all(mconn, load_config(args.config))
        maint.loop(lambda: db.connect(db_path), args.config)
    _th.Thread(target=_maintenance, daemon=True).start()

    print(f"RSSgate listening on http://{cfg['server']['host']}:{cfg['server']['port']}")
    app.run(host=cfg["server"]["host"], port=int(cfg["server"]["port"]),
            threaded=True, debug=False)


if __name__ == "__main__":
    main()
