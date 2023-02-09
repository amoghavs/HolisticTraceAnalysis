# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from typing import Dict, List, Optional

import pandas as pd

from hta.common.trace import Trace
from hta.configs.config import logger
from hta.utils.utils import KernelType, get_kernel_type, get_memory_kernel_type


class TraceCounters:
    def __init__(self):
        pass

    @classmethod
    def _get_queue_length_time_series_for_rank(cls, t: "Trace", rank: int) -> Optional[pd.DataFrame]:
        """
        Returns an (optional) dataframe with time series for the queue length
        on a CUDA streams within requested rank.

        Queue length is defined as the number of outstanding CUDA operations on a stream
        The value of the queue length is:
        1. Incremented when a CUDA runtime operation enqueues a kernel on a stream.
        2. Decremented when a CUDA kernel/memcopy operation executes on a stream.

        Args:
            t (Trace): Input trace data structure.
            rank (int): rank to generate the time series for.

        Returns:
            Optional[pd.DataFrame]
                Returns an (optional) dataframe containing time series with the following
                columns: ts (timestamp), pid, tid (of corresponding GPU, stream), stream and
                queue_length.

                Note that each row or timestamp denotes a change in the value of the
                time series. The value remains constant until the next timestamp.
                In essence, it can be thought of as a step function.
        """
        # get trace for a rank
        trace_df: pd.DataFrame = t.get_trace(rank)

        # cudaLaunchKernel, cudaMemcpyAsync, cudaMemsetAsync
        sym_index = t.symbol_table.get_sym_id_map()
        cudaLaunchKernel_id = sym_index.get("cudaLaunchKernel", None)
        cudaMemcpyAsync_id = sym_index.get("cudaMemcpyAsync", None)
        cudaMemsetAsync_id = sym_index.get("cudaMemsetAsync", None)

        # CUDA Runtime events that may launch kernels
        # - filter events that have a correlated kernel event only.
        runtime_calls: pd.DataFrame = trace_df.query(
            "((name == @cudaMemsetAsync_id) or (name == @cudaMemcpyAsync_id) or "
            " (name == @cudaLaunchKernel_id))"
            "and (index_correlation > 0)"
        ).copy()
        runtime_calls.drop(["stream", "pid", "tid"], axis=1, inplace=True)
        runtime_calls["queue"] = 1

        # GPU kernel events
        gpu_kernels = trace_df[trace_df["stream"].ne(-1)].copy()
        gpu_kernels["queue"] = -1

        # Concat the series of runtime launch events and GPU kernel events
        merged_df = (
            pd.concat(
                [
                    # use the pid, tid and cuda stream from the correlated GPU event.
                    runtime_calls.join(
                        gpu_kernels[["stream", "pid", "tid", "correlation"]].set_index("correlation"),
                        on="correlation",
                    ),
                    gpu_kernels,
                ]
            )
            .sort_values(by="ts")
            .set_index("index")
        )

        result_df_list = []
        for stream, stream_df in merged_df.groupby("stream"):
            logger.debug(f"Processing queue_length for rank {rank}, stream {stream}")
            stream_df["queue_length"] = stream_df["queue"].cumsum()
            result_df_list.append(stream_df)

        return (
            pd.concat(result_df_list)[["ts", "pid", "tid", "stream", "queue_length"]]
            if len(result_df_list) > 0
            else None
        )

    @classmethod
    def get_queue_length_time_series(
        cls,
        t: "Trace",
        ranks: Optional[List[int]] = None,
    ) -> Dict[int, pd.DataFrame]:
        """
        Returns a dictionary of rank -> time series for the queue length of a CUDA stream.

        Queue length is defined as the number of outstanding CUDA operations on a stream
        The value of the queue length is:
        1. Incremented when a CUDA runtime operation enqueues a kernel on a stream.
        2. Decremented when a CUDA kernel/memcopy operation executes on a stream.

        Args:
            t (Trace): Input trace data structure.
            rank (int): rank to perform this analysis for.

        Returns:
            Dict[int, pd.DataFrame]:
                A dictionary of rank -> time series with the queue length of each CUDA stream.
                Each dataframe contains a time series consisting of the following columns:
                ts (timestamp), pid, tid (of corresponding GPU, stream), stream and queue_length.

                Note that each row or timestamp shows a change in the value of the
                time series. The value remains constant until the next timestamp.
                In essence, it can be thought of as a step function.
        """
        if ranks is None or len(ranks) == 0:
            ranks = [0]

        logger.info(
            "Please note that the time series only contains points "
            "when the value changes. Once a values is observed the time series "
            "stays constant until the next update."
        )

        result = {rank: TraceCounters._get_queue_length_time_series_for_rank(t, rank) for rank in ranks}
        return dict(filter(lambda x: x[1] is not None, result.items()))

    @classmethod
    def get_queue_length_summary(
        cls,
        t: "Trace",
        ranks: Optional[List[int]] = None,
    ) -> Optional[pd.DataFrame]:
        """
        Returns an (optional) dataframe with queue length statistics per CUDA stream and rank.

        Args:
            t (Trace): Input trace data structure.
            ranks (list of int): ranks to perform this analysis.

        Returns:
            Optional[pd.DataFrame]
                An (optional) dataframe containing the summary statistics of queue length per
                stream and rank.
        """
        if ranks is None or len(ranks) == 0:
            ranks = [0]

        results_list: List[pd.DataFrame] = []

        for rank, rank_df in TraceCounters.get_queue_length_time_series(t, ranks).items():
            rank_df["rank"] = rank
            result = rank_df[["rank", "stream", "queue_length"]].groupby(["rank", "stream"]).describe()
            results_list.append(result)
        return pd.concat(results_list) if len(results_list) > 0 else None

    @classmethod
    def _get_memory_bw_time_series_for_rank(cls, t: "Trace", rank: int) -> Optional[pd.DataFrame]:
        """
        Returns time series for the memory bandwidth of memory copy and memory set operations
        for specified rank.

        Args:
            t (Trace): Input trace data structure.
            rank (int): rank to generate the time series for.

        Returns:
            Optional[pd.DataFrame]
                Returns an (optional) dataframe with time series for the memory bandwidth.
                The dataframe returned contains time series with columns:
                ts (timestamp), pid (of corresponding GPU), name of memory copy type
                and memory_bw_gbps (memory bandwidth in GB/sec).
        """
        # get trace for a rank
        trace_df: pd.DataFrame = t.get_trace(rank)
        sym_table = t.symbol_table.get_sym_table()

        gpu_kernels = trace_df[trace_df["stream"].ne(-1)].copy()
        gpu_kernels["kernel_type"] = gpu_kernels[["name"]].apply(
            lambda x: get_kernel_type(sym_table[x["name"]]), axis=1
        )

        memcpy_kernels = gpu_kernels[gpu_kernels.kernel_type == KernelType.MEMORY.name].copy()
        memcpy_kernels["name"] = memcpy_kernels[["name"]].apply(
            lambda x: get_memory_kernel_type(sym_table[x["name"]]), axis=1
        )

        # In case of 0 us duration events round it up to 1 us to avoid -ve values
        # see https://github.com/facebookresearch/HolisticTraceAnalysis/issues/20
        memcpy_kernels.loc[memcpy_kernels.dur == 0, ["dur"]] = 1

        membw_time_series_a = memcpy_kernels[["ts", "name", "pid", "memory_bw_gbps"]]
        membw_time_series_b = memcpy_kernels[["ts", "name", "dur", "pid", "memory_bw_gbps"]].copy()

        # The end events have timestamps = start timestamp + duration
        membw_time_series_b.ts = membw_time_series_b.ts + membw_time_series_b.dur
        membw_time_series_b.memory_bw_gbps = -membw_time_series_b.memory_bw_gbps

        membw_time_series = pd.concat(
            [
                membw_time_series_a,
                membw_time_series_b[["ts", "pid", "name", "memory_bw_gbps"]],
            ],
            ignore_index=True,
        ).sort_values(by="ts")

        result_df_list = []
        for _, membw_df in membw_time_series.groupby("name"):
            membw_df.memory_bw_gbps = membw_df.memory_bw_gbps.cumsum()
            result_df_list.append(membw_df)

        if len(result_df_list) == 0:
            return None

        result_df = pd.concat(result_df_list)[["ts", "pid", "name", "memory_bw_gbps"]]
        return result_df

    @classmethod
    def get_memory_bw_time_series(
        cls,
        t: "Trace",
        ranks: Optional[List[int]] = None,
    ) -> Dict[int, pd.DataFrame]:
        """
        Returns a dictionary of rank -> time series for the memory bandwidth.

        Args:
            t (Trace): Input trace data structure.
            ranks (list of int): ranks to perform this analysis for.

        Returns:
            Dict[int, pd.DataFrame]
                Returns a dictionary of rank -> time series for the memory bandwidth.
                The dataframe returned contains time series along with the following columns:
                ts (timestamp), pid (of corresponding GPU), name of memory copy type
                and memory_bw_gbps (memory bandwidth in GB/sec).
        """
        if ranks is None or len(ranks) == 0:
            ranks = [0]

        logger.info(
            "Please note that the time series only contains points "
            "when the value changes. Once a values is observed the time series "
            "stays constant until the next update."
        )
        result = {rank: TraceCounters._get_memory_bw_time_series_for_rank(t, rank) for rank in ranks}
        return dict(filter(lambda x: x[1] is not None, result.items()))

    @classmethod
    def get_memory_bw_summary(
        cls,
        t: "Trace",
        ranks: Optional[List[int]] = None,
    ) -> Optional[pd.DataFrame]:
        """
        Returns an (optional) dataframe containing the summary statistics of memory ops. The
        tracked memory ops are MemcpyDtoH, MemcpyHtoD, MemcpyDtoD and MemSet.

        Args:
            t (Trace): Input trace data structure.
            ranks (list of int): ranks to perform this analysis for.

        Returns:
            Optional[pd.DataFrame]
                An (optional) dataframe containing the summary statistics of the following memory ops:
                MemcpyDtoH, MemcpyHtoD, MemcpyDtoD, MemSet.
        """
        if ranks is None or len(ranks) == 0:
            ranks = [0]

        results_list: List[pd.DataFrame] = []

        for rank, rank_df in TraceCounters.get_memory_bw_time_series(t, ranks).items():
            rank_df["rank"] = rank
            # Exclude the 0 points in time series
            rank_df = rank_df[rank_df.memory_bw_gbps > 0]

            result = rank_df[["rank", "name", "memory_bw_gbps"]].groupby(["rank", "name"]).describe()
            results_list.append(result)
        return pd.concat(results_list) if len(results_list) > 0 else None
