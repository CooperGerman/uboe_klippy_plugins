#!/usr/bin/env python3

import argparse
import json
import os


def load_samples(path):
    samples = []
    with open(path, "r") as input_file:
        for line_number, line in enumerate(input_file, 1):
            try:
                record = json.loads(line)
            except ValueError as error:
                raise ValueError(
                    "invalid JSON on line %d: %s" % (line_number, error))
            if not isinstance(record, dict):
                raise ValueError(
                    "JSONL line %d is not an object" % line_number)
            if record.get("type") == "sample":
                samples.append(record)
    if len(samples) < 2:
        raise ValueError("at least two bed-mesh samples are required")
    return samples


def prepare_frames(samples, mode):
    import numpy

    baseline = numpy.asarray(samples[0]["probed_matrix"], dtype=float)
    if baseline.ndim != 2 or not baseline.size:
        raise ValueError("first sample contains an invalid probed matrix")

    frames = []
    for sample in samples:
        matrix = numpy.asarray(sample["probed_matrix"], dtype=float)
        if matrix.shape != baseline.shape:
            raise ValueError(
                "sample %s has a different mesh shape"
                % sample.get("sample_index"))
        if mode == "absolute":
            frame = matrix
        elif mode == "baseline":
            frame = matrix - baseline
        elif mode == "previous":
            delta = sample.get("delta_from_previous")
            if delta is None:
                frame = numpy.zeros_like(matrix)
            else:
                frame = numpy.asarray(delta, dtype=float)
                if frame.shape != baseline.shape:
                    raise ValueError(
                        "sample %s has an invalid delta matrix"
                        % sample.get("sample_index"))
        else:
            raise ValueError("unknown render mode: %s" % mode)
        frames.append(frame)
    return numpy.asarray(frames)


def render_gif(samples, output_path, mode="baseline", fps=1.0, dpi=120):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import animation
    import matplotlib.pyplot as pyplot
    import numpy

    frames = prepare_frames(samples, mode)
    if not numpy.isfinite(frames).all():
        raise ValueError("sample data contains non-finite values")

    params = samples[0].get("mesh_params", {})
    extent = (
        float(params["min_x"]),
        float(params["max_x"]),
        float(params["min_y"]),
        float(params["max_y"]),
    ) if all(key in params for key in ("min_x", "max_x",
                                      "min_y", "max_y")) else None

    minimum = float(frames.min())
    maximum = float(frames.max())
    if mode in ("baseline", "previous"):
        limit = max(abs(minimum), abs(maximum), .001)
        norm = matplotlib.colors.TwoSlopeNorm(
            vmin=-limit, vcenter=0., vmax=limit)
        cmap = "coolwarm"
    else:
        padding = max((maximum - minimum) * .05, .001)
        norm = matplotlib.colors.Normalize(
            vmin=minimum - padding, vmax=maximum + padding)
        cmap = "viridis"

    figure, axes = pyplot.subplots(figsize=(9, 7), constrained_layout=True)
    image = axes.imshow(
        frames[0], origin="lower", interpolation="bilinear", aspect="auto",
        extent=extent, cmap=cmap, norm=norm)
    colorbar = figure.colorbar(image, ax=axes)
    colorbar.set_label("Z or delta (mm)")
    axes.set_xlabel("X (mm)")
    axes.set_ylabel("Y (mm)")

    def update(frame_index):
        sample = samples[frame_index]
        image.set_data(frames[frame_index])
        metric = sample.get("metrics") or {}
        maximum_delta = metric.get("max_abs_delta_mm")
        suffix = ""
        if maximum_delta is not None:
            suffix = " | max |delta| %.4f mm" % maximum_delta
        axes.set_title(
            "Bed expansion - %s | sample %d | %.1f s%s"
            % (
                mode,
                sample.get("sample_index", frame_index),
                sample.get("elapsed_s", 0.),
                suffix,
            ))
        return (image,)

    update(0)
    animation_writer = animation.PillowWriter(
        fps=fps, metadata={"title": "Bed expansion analysis"})
    output_path = os.path.abspath(os.path.expanduser(output_path))
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    movie = animation.FuncAnimation(
        figure, update, frames=len(samples),
        interval=1000. / fps, blit=False)
    try:
        movie.save(output_path, writer=animation_writer, dpi=dpi)
    finally:
        pyplot.close(figure)
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Render a bed expansion JSONL run as an animated GIF.")
    parser.add_argument("run", help="Path to the bed expansion JSONL file")
    parser.add_argument(
        "--mode", choices=("absolute", "baseline", "previous"),
        default="baseline",
        help="absolute mesh, delta from first sample, or delta from prior sample")
    parser.add_argument(
        "--output", help="Output GIF path (default: beside the JSONL file)")
    parser.add_argument(
        "--fps", type=float, default=1.0,
        help="Animation frames per second")
    parser.add_argument("--dpi", type=int, default=120)
    args = parser.parse_args()
    if args.fps <= 0:
        parser.error("--fps must be above zero")
    if args.dpi <= 0:
        parser.error("--dpi must be above zero")

    output = args.output
    if output is None:
        base, _ = os.path.splitext(args.run)
        output = "%s_%s.gif" % (base, args.mode)
    samples = load_samples(args.run)
    print(render_gif(
        samples, output, mode=args.mode, fps=args.fps, dpi=args.dpi))


if __name__ == "__main__":
    main()
