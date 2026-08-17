"""CLI du pipeline : ``python -m audio_cleaning.cli <commande> [options]``.

Commandes :

- ``run``       : nettoie une vidéo (ou un dossier de vidéos) et écrit les sorties.
- ``evaluate``  : calcule matrice de confusion + métriques d'un fichier annoté.
- ``make-eval`` : génère un gabarit d'annotation depuis un ``segments.json``.
- ``benchmark`` : compare deux configurations (ex. Silero seul vs Silero + YAMNet)
  sur la même entrée et affiche RTF / durée retirée / speech ratio.

Toute clé de config est surchargeable via ``--set section.clé=valeur`` (répétable).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import apply_overrides, load_config
from .evaluation import (
    evaluate,
    save_evaluation,
    segments_to_eval_rows,
    write_eval_template,
)
from .pipeline import CleaningPipeline
from .segments import Segment


def _load_cfg(args: argparse.Namespace):
    cfg = load_config(args.config)
    if getattr(args, "set", None):
        apply_overrides(cfg, args.set)
    if getattr(args, "output", None):
        cfg.output.root = args.output
    return cfg


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    pipeline = CleaningPipeline(cfg)

    jobs = _discover_jobs(Path(args.input), args.transcript)
    if not jobs:
        print(f"aucune entrée audio trouvée sous {args.input!r}", file=sys.stderr)
        return 1

    failures = 0
    for audio_path, video_id, transcript in jobs:
        try:
            res = pipeline.process(audio_path, video_id=video_id, transcript_path=transcript)
            c = res.metrics["cleaning"]
            print(
                f"[ok] {video_id}: {c['original_duration_s']}s -> {c['cleaned_duration_s']}s "
                f"(retiré {c['cleaning_ratio']:.0%}, RTF {res.metrics['performance']['rtf']}) "
                f"-> {res.output_dir}"
            )
        except Exception as exc:  # noqa: BLE001 - on continue sur le lot
            failures += 1
            print(f"[échec] {video_id}: {exc}", file=sys.stderr)
    return 1 if failures and failures == len(jobs) else 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    result = evaluate(args.annotations)
    out = args.output or "evaluation.json"
    save_evaluation(out, result)
    q = result["quality"]
    print(f"segments annotés : {result['num_annotated_segments']}")
    print(f"precision={q['precision']}  recall={q['recall']}  f1={q['f1_score']}")
    print(
        f"speech_retention={q['speech_retention']}  "
        f"false_speech_deletion_rate={q['false_speech_deletion_rate']}"
    )
    print(f"-> {out}")
    return 0


def _cmd_make_eval(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.segments).read_text(encoding="utf-8"))
    segments = [
        Segment(
            start=s["start"],
            end=s["end"],
            label=s["label"],
            action=s.get("action", "keep"),
            confidence=s.get("confidence", 1.0),
        )
        for s in raw
    ]
    rows = segments_to_eval_rows(args.video_id, segments)
    path = write_eval_template(args.output, rows)
    print(f"gabarit d'annotation écrit ({len(rows)} lignes) -> {path}")
    return 0


def _cmd_benchmark(args: argparse.Namespace) -> int:
    """Compare plusieurs configs sur la même entrée (voir README §benchmark)."""
    configs = {
        "silero_only": ["classification.backend=heuristic"],  # VAD seul, pas de YAMNet
        "silero_yamnet": [],  # config par défaut
    }
    if args.configs:
        configs = {
            name: overrides.split(",") if overrides else []
            for name, overrides in (c.split(":", 1) for c in args.configs)
        }

    print(f"{'config':<16} {'orig(s)':>9} {'clean(s)':>9} {'retiré':>7} {'speech%':>8} {'RTF':>7}")
    for name, overrides in configs.items():
        cfg = load_config(args.config)
        apply_overrides(cfg, overrides)
        cfg.output.root = str(Path(args.output or "outputs") / f"bench_{name}")
        cfg.output.save_visualization = False
        res = CleaningPipeline(cfg).process(
            args.input, video_id=args.video_id, transcript_path=args.transcript
        )
        c, v, p = res.metrics["cleaning"], res.metrics["vad"], res.metrics["performance"]
        print(
            f"{name:<16} {c['original_duration_s']:>9} {c['cleaned_duration_s']:>9} "
            f"{c['cleaning_ratio']:>7.0%} {v['speech_ratio']:>8.0%} {p['rtf']:>7}"
        )
    return 0


def _discover_jobs(
    input_path: Path, transcript_arg: str | None
) -> list[tuple[Path, str, str | None]]:
    """Résout l'entrée en liste de ``(audio, video_id, transcript)``.

    - fichier      -> une seule vidéo (video_id = nom sans extension) ;
    - dossier      -> chaque audio trouvé ; transcript = ``transcript.json`` voisin.
    """
    audio_exts = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".webm", ".mp4"}
    if input_path.is_file():
        return [(input_path, input_path.stem, transcript_arg)]
    jobs: list[tuple[Path, str, str | None]] = []
    for audio in sorted(input_path.rglob("*")):
        if audio.suffix.lower() in audio_exts:
            sidecar = audio.with_name("transcript.json")
            jobs.append(
                (audio, audio.parent.name or audio.stem, str(sidecar) if sidecar.exists() else None)
            )
    return jobs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audio_cleaning", description=__doc__)
    parser.add_argument("--config", help="chemin de config.yaml (défaut : celui du package)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="nettoie une vidéo ou un dossier")
    run.add_argument("input", help="fichier audio ou dossier de vidéos")
    run.add_argument("--transcript", help="transcript JSON (si input est un fichier)")
    run.add_argument("--output", help="dossier de sortie (surcharge output.root)")
    run.add_argument("--set", action="append", default=[], help="surcharge section.clé=valeur")
    run.set_defaults(func=_cmd_run)

    ev = sub.add_parser("evaluate", help="métriques depuis un fichier annoté")
    ev.add_argument("annotations", help="CSV/JSON annoté (true_label/predicted_label)")
    ev.add_argument("--output", help="fichier de sortie (défaut evaluation.json)")
    ev.set_defaults(func=_cmd_evaluate)

    mk = sub.add_parser("make-eval", help="gabarit d'annotation depuis segments.json")
    mk.add_argument("segments", help="chemin d'un segments.json")
    mk.add_argument("--video-id", required=True)
    mk.add_argument("--output", required=True, help="gabarit .csv ou .json à écrire")
    mk.set_defaults(func=_cmd_make_eval)

    bench = sub.add_parser("benchmark", help="compare des configs sur une entrée")
    bench.add_argument("input", help="fichier audio")
    bench.add_argument("--video-id", default="benchmark")
    bench.add_argument("--transcript")
    bench.add_argument("--output")
    bench.add_argument(
        "--configs",
        nargs="*",
        help="nom:override1,override2 (défaut: silero_only vs silero_yamnet)",
    )
    bench.set_defaults(func=_cmd_benchmark)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
