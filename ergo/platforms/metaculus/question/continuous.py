from collections import namedtuple
from datetime import datetime
import textwrap
from typing import Dict, List, Optional, Union

import jax.numpy as np
import numpy as onp
import pandas as pd
from plotnine import (
    aes,
    geom_density,
    ggplot,
    ggtitle,
    labs,
    scale_fill_brewer,
    scale_x_continuous,
)
import requests

from ergo import ppl
import ergo.distributions as dist
from ergo.scale import Scale
from ergo.theme import ergo_theme
from ergo.utils import memoized_method

from .constants import (
    max_loc,
    max_open_high,
    max_open_low,
    max_scale,
    min_open_high,
    min_open_low,
    min_scale,
)
from .question import MetaculusQuestion
from .types import ArrayLikes

Bounds = namedtuple("Bounds", ["floor", "ceiling"])


class ContinuousQuestion(MetaculusQuestion):
    """
    A continuous Metaculus question -- a question of the form,
    what's your distribution on this event?
    """

    def side_open(self, side) -> bool:
        try:
            return self.possibilities[side] == "tail"

        # The Metaculus API is inconsistent about how open sides are expressed.
        # Many questions express it explicitly via possibilities[side],
        # e.g. https://www.metaculus.com/questions/3992.
        # Other questions (I think only older ones)
        # do not seem to explicitly state whether the sides are open,
        # e.g. https://www.metaculus.com/questions/605.
        # My current guess (reflected in the code below)
        # is that both sides are always closed
        # on questions where possibilities[side] is missing.
        except KeyError:
            return False

    @property
    def low_open(self) -> bool:
        """
        Are you allowed to place probability mass below the bottom
        of this question's range?
        """
        return self.side_open("low")

    @property
    def high_open(self) -> bool:
        """
        Are you allowed to place probability mass
        above the top of this question's range?
        """
        return self.side_open("high")

    @property
    def p_above(self) -> Optional[float]:
        if self.latest_community_percentiles is None:
            return None
        return 1 - self.latest_community_percentiles["high"]

    @property
    def p_below(self) -> Optional[float]:
        if self.latest_community_percentiles is None:
            return None
        return self.latest_community_percentiles["low"]

    @property
    def p_outside(self) -> Optional[float]:
        """
        How much probability mass is outside this question's range?
        """
        if self.p_below is None or self.p_above is None:
            return None
        return self.p_below + self.p_above

    @property
    def has_predictions(self):
        """
        Are there any predictions for the question yet?
        """
        return hasattr(self, "prediction_histogram")

    @property
    def question_range(self):
        """
        Range of answers specified when the question was created
        """
        return self.possibilities["scale"]

    @property
    def question_range_width(self):
        return self.question_range["max"] - self.question_range["min"]

    def _scale_x(self, xmin: float = None, xmax: float = None):
        return scale_x_continuous(limits=(xmin, xmax))

    @property
    def plot_title(self):
        return "\n".join(textwrap.wrap(self.name or self.data["title"], 60))  # type: ignore

    @property
    def latest_community_percentiles(self):
        """
        :return: Some percentiles for the metaculus commununity's latest rough
            prediction. `prediction_histogram` returns a more fine-grained
            histogram of the community prediction
        """
        if len(self.prediction_timeseries) == 0:
            return None

        return self.prediction_timeseries[-1]["community_prediction"]

    def prepare_logistic(self, normalized_dist: dist.Logistic) -> dist.Logistic:
        """
        Transform a single logistic distribution by clipping the
        parameters and adding scale information as needed for submission to
        Metaculus. The loc and scale have to be within a certain range
        for the Metaculus API to accept the prediction.

        :param dist: a (normalized) logistic distribution
        :return: a transformed logistic distribution
        """
        if hasattr(normalized_dist, "base_dist"):
            normalized_dist = normalized_dist.base_dist  # type: ignore

        if normalized_dist.s <= 0:
            raise ValueError("logistic_params.scale must be greater than 0")

        clipped_loc = min(normalized_dist.loc, max_loc)
        clipped_scale = float(onp.clip(normalized_dist.s, min_scale, max_scale))  # type: ignore

        if self.low_open:
            low = float(onp.clip(normalized_dist.cdf(0), min_open_low, max_open_low,))
        else:
            low = 0

        if self.high_open:
            high = float(
                onp.clip(normalized_dist.cdf(1), min_open_high + low, max_open_high,)
            )
        else:
            high = 1

        return dist.Logistic(
            clipped_loc, clipped_scale, Scale(0, 1), {"low": low, "high": high}
        )

    def prepare_logistic_mixture(
        self, normalized_dist: dist.LogisticMixture
    ) -> dist.LogisticMixture:
        """
        Transform a (normalized) logistic mixture distribution as
        needed for submission to Metaculus.

        :param normalized_dist: normalized mixture dist
        :return: normalized dist clipped and formatted for the API
        """
        transformed_components = [
            self.prepare_logistic(c) for c in normalized_dist.components
        ]

        transformed_probs = onp.clip(normalized_dist.probs, 0.01, 0.99)  # type: ignore

        return dist.LogisticMixture(transformed_components, transformed_probs)  # type: ignore

    def community_pairs(self, normalized=False, denorm_xs_only=False):
        if normalized:
            return [
                {"x": float(v[0]), "density": v[2]} for v in self.prediction_histogram
            ]

        elif denorm_xs_only:
            return [
                {"x": self.scale.denormalize_point(float(v[0])), "density": v[2]}
                for v in self.prediction_histogram
            ]

        else:
            return [
                {
                    "x": self.scale.denormalize_point(float(v[0])),
                    "density": self.scale.denormalize_density(float(v[2])),
                }
                for v in self.prediction_histogram
            ]

    def community_dist(self) -> dist.PointDensity:
        """
        Get the community distribution for this question
        NB: currently missing the part of the distribtion outside the question range

        :return: the (true-scale) community distribution as a histogram.
        """
        # TODO (#306): Unify distributions interface
        # TODO (#307): Account for values out of range in
        #   ContinuousQuestion.community_dist()

        histogram = self.community_pairs(normalized=True)
        return dist.PointDensity.from_pairs(histogram, self.scale, normalized=True)

    def community_conditions(self, crossentropy_weight=0.1, interval_weight=10000.0):
        from ergo.conditions import (
            CrossEntropyCondition,
            IntervalCondition,
            Condition,
        )

        pairs = self.community_pairs(normalized=True)

        # Note that this histogram is normalized - it sums to 1 even if the pairs don't!
        point_density_dist = dist.PointDensity.from_pairs(
            pairs, scale=self.scale, normalized=True
        )

        condition = CrossEntropyCondition(
            point_density_dist, weight=crossentropy_weight
        )

        community_conditions: List[Condition] = [condition]

        if self.low_open:
            community_conditions.append(
                IntervalCondition(
                    p=self.p_below, max=self.scale.low, weight=interval_weight
                )
            )

        if self.high_open:
            community_conditions.append(
                IntervalCondition(
                    p=self.p_above, min=self.scale.high, weight=interval_weight
                )
            )

        return community_conditions

    @memoized_method(None)
    def community_dist_in_range(self):
        """
        A distribution for the portion of the current normalized community prediction
        that's within the question's range, i.e. 0...(len(self.prediction_histogram)-1).

        :return: distribution on integers
        """
        y2 = [p[2] for p in self.prediction_histogram]
        return dist.Categorical(np.array(y2))

    def sample_normalized_community(self) -> float:
        """
        Sample an approximation of the entire current community prediction,
        on the normalized scale. The main reason that it's just an approximation
        is that we don't know exactly where probability mass outside of the question
        range should be, so we place it arbitrarily.

        :return: One sample on the normalized scale
        """

        # FIXME: Samples below/above range are pretty arbitrary
        sample_below_range = -dist.halfnormal(0.1)
        sample_above_range = 1 + dist.halfnormal(0.1)
        sample_in_range = ppl.sample(self.community_dist_in_range()) / float(
            len(self.prediction_histogram)
        )
        p_below = self.latest_community_percentiles["low"]
        p_above = 1 - self.latest_community_percentiles["high"]
        p_in_range = 1 - p_below - p_above
        return float(
            dist.random_choice(
                [sample_below_range, sample_in_range, sample_above_range],
                ps=[p_below, p_in_range, p_above],
            )
        )

    def sample_community(self) -> float:
        """
        Sample an approximation of the entire current community prediction,
        on the true scale of the question.
        The main reason that it's just an approximation is that we don't know
        exactly where probability mass outside of the question range should be,
        so we place it arbitrarily

        :return: One sample on the true scale
        """

        if not self.has_predictions:
            raise ValueError("There are currently no predictions for this question")
        normalized_sample = self.sample_normalized_community()
        sample = np.array(self.scale.denormalize_points([normalized_sample]))
        if self.name:
            ppl.tag(sample, self.name)
        return float(sample)

    def get_submission_from_samples(
        self, samples: Union[pd.Series, np.ndarray], verbose=False
    ) -> dist.LogisticMixture:
        if not type(samples) in ArrayLikes:
            raise TypeError("Please submit a vector of samples")

        normalized_samples = self.scale.normalize_points(samples)
        _dist = dist.LogisticMixture.from_samples(
            normalized_samples, fixed_params={"num_components": 3}, verbose=verbose
        )
        return self.prepare_logistic_mixture(_dist)

    @staticmethod
    def format_logistic_for_api(submission: dist.Logistic, weight: float) -> dict:
        if submission.scale is None:
            raise ValueError("Submission distribution needs a scale")
        # Convert all the numbers to floats here so that you can be sure that
        # wherever they originated (e.g. numpy), they'll be regular old floats that
        # can be converted to json by json.dumps.
        return {
            "kind": "logistic",
            "x0": float(submission.loc),
            "s": float(submission.s),
            "w": float(weight),
            "low": float(submission.metadata["low"]),
            "high": float(submission.metadata["high"]),
        }

    def submit(self, submission: dist.LogisticMixture) -> requests.Response:

        prediction_data = {
            "prediction": {
                "kind": "multi",
                "d": [
                    self.format_logistic_for_api(c, submission.probs[i])
                    for i, c in enumerate(submission.components)
                ],
            },
            "void": False,
        }

        r = self.metaculus.predict(self.id, prediction_data,)

        self.refresh_question()

        return r

    def submit_from_samples(self, samples, verbose=False) -> requests.Response:
        """
        Submit prediction to Metaculus based on samples from a prediction distribution

        :param samples: Samples from a distribution answering the prediction question
        :return: logistic mixture params clipped and formatted to submit to Metaculus
        """
        submission = self.get_submission_from_samples(samples, verbose=verbose)
        return self.submit(submission)

    def get_bounds(self):
        # Return true-scale bounds
        floor, ceiling = None, None
        possibilities = self.possibilities
        if possibilities.get("low") != "tail":
            floor = float(self.scale.low)
        if possibilities.get("high") != "tail":
            ceiling = float(self.scale.high)
        return Bounds(floor=floor, ceiling=ceiling)

    def get_logistic_from_json(self, logistic_json: Dict) -> dist.Logistic:
        bounds = self.get_bounds()
        normed_bounds = {}
        if bounds.floor is not None:
            normed_bounds["floor"] = self.scale.normalize_point(bounds.floor)
        if bounds.ceiling is not None:
            normed_bounds["ceiling"] = self.scale.normalize_point(bounds.ceiling)
        return dist.Truncate(  # type: ignore
            dist.Logistic(logistic_json["x0"], logistic_json["s"], normalized=True),
            **normed_bounds,
        )

    def get_submission_from_json(self, submission_json: Dict) -> dist.LogisticMixture:
        components = [
            self.get_logistic_from_json(logistic_json)
            for logistic_json in submission_json
        ]

        probs = [logistic_json["w"] for logistic_json in submission_json]
        return dist.LogisticMixture(components, probs)

    def get_latest_normalized_prediction(self) -> dist.LogisticMixture:
        latest_prediction = self.my_predictions["predictions"][-1]["d"]
        return self.get_submission_from_json(latest_prediction)

    def show_prediction(
        self,
        samples,
        plot_samples: bool = True,
        plot_fitted: bool = False,
        percent_kept: float = 0.95,
        side_cut_from: str = "both",
        show_community: bool = False,
        num_samples: int = 1000,
        **kwargs,
    ):
        """
        Plot prediction on the true question scale from samples or a submission
        object. Optionally compare prediction against a sample from the distribution
        of community predictions

        :param samples: samples from a distribution answering the prediction question
            (true scale). Can either be a 1-d array corresponding to one model's
            predictions, or a pandas DataFrame with each column corresponding to
            a distinct model's predictions
        :param plot_samples: boolean indicating whether to plot the raw samples
        :param plot_fitted: boolean indicating whether to compute Logistic Mixture
            Params from samples and plot the resulting fitted distribution. Note
            this is currently only supported for 1-d samples
        :param percent_kept: percentage of sample distrubtion to keep
        :param side_cut_from: which side to cut tails from,
            either 'both','lower', or 'upper'
        :param show_community: boolean indicating whether comparison
            to community predictions should be made
        :param num_samples: number of samples from the community
        :param kwargs: additional plotting parameters
        """

        df = pd.DataFrame()

        if not plot_fitted and not plot_samples:
            raise ValueError(
                "Nothing to plot. Niether plot_fitted nor plot_samples was True"
            )

        if plot_samples:
            if isinstance(samples, list):
                samples = pd.Series(samples)
            if not type(samples) in ArrayLikes:
                raise ValueError(
                    "Samples should be a list, numpy array or pandas series"
                )
            num_samples = samples.shape[0]

            if type(samples) == pd.DataFrame:
                if plot_fitted and samples.shape[1] > 1:
                    raise ValueError(
                        "For multiple predictions comparisons, only samples can be compared (plot_fitted must be False)"
                    )
                for col in samples:
                    df[col] = self.scale.normalize_points(samples[col])
            else:
                df["samples"] = self.scale.normalize_points(samples)

        if plot_fitted:
            prediction = self.get_submission_from_samples(samples)
            df["fitted"] = pd.Series(
                [prediction.sample() for _ in range(0, num_samples)]
            )

        if show_community:
            df["community"] = [  # type: ignore
                self.sample_normalized_community() for _ in range(0, num_samples)
            ]

        # get domain for graph given the percentage of distribution kept
        xmin, xmax = self.scale.denormalize_points(
            self.get_central_quantiles(
                df, percent_kept=percent_kept, side_cut_from=side_cut_from,
            )
        )

        for col in df:
            df[col] = self.scale.denormalize_points(df[col])

        df = pd.melt(df, var_name="sources", value_name="samples")  # type: ignore

        plot = self.comparison_plot(df, xmin, xmax, **kwargs) + labs(
            x="Prediction",
            y="Density",
            title=self.plot_title + "\n\nPrediction vs Community"
            if show_community
            else self.plot_title,
        )
        try:
            plot.draw()  # type: ignore
        except RuntimeError as err:
            print(err)
            print(
                "The plot was unable to automatically determine a bandwidth. You can manually specify one with the keyword 'bw', e.g., show_prediction(..., bw=.1)"
            )

    def show_community_prediction(
        self,
        percent_kept: float = 0.95,
        side_cut_from: str = "both",
        num_samples: int = 1000,
        **kwargs,
    ):
        """
        Plot samples from the community prediction on this question

        :param percent_kept: percentage of sample distrubtion to keep
        :param side_cut_from: which side to cut tails from,
            either 'both','lower', or 'upper'
        :param num_samples: number of samples from the community
        :param kwargs: additional plotting parameters
        """
        community_samples = pd.Series(
            [self.sample_normalized_community() for _ in range(0, num_samples)]
        )

        _xmin, _xmax = self.scale.denormalize_points(
            self.get_central_quantiles(
                community_samples,
                percent_kept=percent_kept,
                side_cut_from=side_cut_from,
            )
        )

        df = pd.DataFrame(
            data={"samples": self.scale.denormalize_points(community_samples)}
        )

        plot = self.density_plot(df, _xmin, _xmax, **kwargs) + labs(
            x="Prediction",
            y="Density",
            title=self.plot_title + "\n\nCommunity Predictions",
        )
        try:
            plot.draw()  # type: ignore
        except RuntimeError as err:
            print(err)
            print(
                "The plot was unable to automatically determine a bandwidth. You can manually specify one with the keyword 'bw', e.g., show_prediction(..., bw=.1)"
            )

    def comparison_plot(
        self, df: pd.DataFrame, xmin=None, xmax=None, bw="normal_reference", **kwargs
    ):
        return (
            ggplot(df, aes(df.columns[1], fill=df.columns[0]))
            + scale_fill_brewer(type="qual", palette="Pastel1")
            + geom_density(bw=bw, alpha=0.8)
            + ggtitle(self.plot_title)
            + self._scale_x(xmin, xmax)
            + ergo_theme
        )

    def density_plot(
        self,
        df: pd.DataFrame,
        xmin=None,
        xmax=None,
        fill: str = "#fbb4ae",
        bw="normal_reference",
        **kwargs,
    ):
        return (
            ggplot(df, aes(df.columns[0]))
            + geom_density(fill=fill, alpha=0.8)
            + ggtitle(self.plot_title)
            + self._scale_x(xmin, xmax)
            + ergo_theme
        )

    def change_since(self, since: datetime):
        """
        Calculate change in community prediction median between the argument and most
        recent prediction

        :param since: datetime
        :return: change in median community prediction since datetime
        """
        try:
            old = self.get_community_prediction(before=since)
            new = self.get_community_prediction()
        except LookupError:
            return 0

        return new["q2"] - old["q2"]

    def normalize_samples(self, samples):
        """
        Map samples from their true scale to the Metaculus normalized scale
        :param samples: samples from a distribution answering the prediction question
            (true scale)
        :return: samples on the normalized scale
        """
        return self.scale.normalize_points(samples)

    def denormalize_samples(self, samples):
        """
        Map samples from the Metaculus normalized scale to the true scale
        :param samples: samples on the normalized scale
        :return: samples from a distribution answering the prediction question
            (true scale)
        """

        return self.scale.denormalize_points(samples)
