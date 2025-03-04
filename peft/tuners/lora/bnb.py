# coding=utf-8
# Copyright 2023-present the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import warnings
from typing import List, Optional

import bitsandbytes as bnb
import torch

from peft.import_utils import is_bnb_4bit_available, is_bnb_available
from peft.tuners.tuners_utils import BaseTunerLayer
from peft.utils.other import transpose

from .layer import LoraLayer


if is_bnb_available():

    class Linear8bitLt(torch.nn.Module, LoraLayer):
        # Lora implemented in a dense layer
        def __init__(
            self,
            base_layer: torch.nn.Module,
            adapter_name: str,
            r: int = 0,
            lora_alpha: int = 1,
            lora_dropout: float = 0.0,
            init_lora_weights: bool = True,
            use_rslora: bool = False,
            **kwargs,
        ) -> None:
            super().__init__()
            LoraLayer.__init__(self, base_layer)



            ##### sparse lora add
            weight = self.get_base_layer().weight
            state = self.get_base_layer().state
            if state.SCB is None:
                state.SCB = weight.SCB

            # Dequantize the result of identity matrix and int8 weight because bitsandbytes does not support int8
            # dequantization directly
            im = torch.eye(weight.data.shape[-1]).contiguous().half().to(weight.device)
            im, imt, SCim, SCimt, coo_tensorim = bnb.functional.double_quant(im)
            im, Sim = bnb.functional.transform(im, "col32")
            if state.CxB is None:
                state.CxB, state.SB = bnb.functional.transform(weight.data, to_order=state.formatB)
            out32, Sout32 = bnb.functional.igemmlt(im, state.CxB, Sim, state.SB)
            output = bnb.functional.mm_dequant(out32, Sout32, SCim, state.SCB, bias=None).t()

            w_data = output.float()

            self.mask = (1-(w_data==0).int()).bool().cuda()

            # count = (w_data==0).sum().item()
            # total_params = w_data.numel()
            # print(f"layer sparsity {float(count)/total_params:.4f}")
            # print(mask)
            # import jbhvg

            ##### sparse lora add



            self._active_adapter = adapter_name
            self.update_layer(adapter_name, r, lora_alpha, lora_dropout, init_lora_weights, use_rslora)

        def merge(self, safe_merge: bool = False, adapter_names: Optional[List[str]] = None) -> None:
            """
            Merge the active adapter weights into the base weights

            Args:
                safe_merge (`bool`, *optional*):
                    If True, the merge operation will be performed in a copy of the original weights and check for NaNs
                    before merging the weights. This is useful if you want to check if the merge operation will produce
                    NaNs. Defaults to `False`.
                adapter_names (`List[str]`, *optional*):
                    The list of adapter names that should be merged. If None, all active adapters will be merged.
                    Defaults to `None`.
            """
            if self.merged:
                warnings.warn(
                    f"Already following adapters were merged {','.join(self.merged_adapters)}. "
                    f"You are now additionally merging {','.join(self.active_adapters)}."
                )

            if adapter_names is None:
                adapter_names = self.active_adapters

            for active_adapter in adapter_names:
                if active_adapter not in self.lora_A.keys():
                    continue
                warnings.warn(
                    "Merge lora module to 8-bit linear may get different generations due to rounding errors."
                )
                lora_data = self.get_delta_weight(active_adapter)

                weight = self.get_base_layer().weight
                state = self.get_base_layer().state
                if state.SCB is None:
                    state.SCB = weight.SCB

                # Dequantize the result of identity matrix and int8 weight because bitsandbytes does not support int8
                # dequantization directly
                im = torch.eye(weight.data.shape[-1]).contiguous().half().to(weight.device)
                im, imt, SCim, SCimt, coo_tensorim = bnb.functional.double_quant(im)
                im, Sim = bnb.functional.transform(im, "col32")
                if state.CxB is None:
                    state.CxB, state.SB = bnb.functional.transform(weight.data, to_order=state.formatB)
                out32, Sout32 = bnb.functional.igemmlt(im, state.CxB, Sim, state.SB)
                output = bnb.functional.mm_dequant(out32, Sout32, SCim, state.SCB, bias=None).t()

                w_data = output.to(lora_data.dtype).to(lora_data.device) + lora_data
                if safe_merge and not torch.isfinite(w_data).all():
                    raise ValueError(
                        f"NaNs detected in the merged weights. The adapter {active_adapter} seems to be broken"
                    )

                self.get_base_layer().weight = bnb.nn.Int8Params(
                    w_data.to("cpu"), requires_grad=False, has_fp16_weights=weight.has_fp16_weights
                ).to(weight.device)
                state.reset_grads()
                self.merged_adapters.append(active_adapter)

        def unmerge(self) -> None:
            """
            This method unmerges all merged adapter layers from the base weights.
            """
            if not self.merged:
                warnings.warn("Already unmerged. Nothing to do.")
                return

            while len(self.merged_adapters) > 0:
                active_adapter = self.merged_adapters.pop()
                if active_adapter not in self.lora_A.keys():
                    continue
                warnings.warn(
                    "Unmerge lora module to 8-bit linear may get different generations due to rounding errors."
                )
                lora_data = self.get_delta_weight(active_adapter)

                weight = self.get_base_layer().weight
                state = self.get_base_layer().state
                if state.SCB is None:
                    state.SCB = weight.SCB
                im = torch.eye(weight.data.shape[-1]).contiguous().half().to(weight.device)
                im, imt, SCim, SCimt, coo_tensorim = bnb.functional.double_quant(im)
                im, Sim = bnb.functional.transform(im, "col32")

                if state.CxB is None:
                    state.CxB, state.SB = bnb.functional.transform(weight.data, to_order=state.formatB)
                out32, Sout32 = bnb.functional.igemmlt(im, state.CxB, Sim, state.SB)
                output = bnb.functional.mm_dequant(out32, Sout32, SCim, state.SCB, bias=None).t()

                w_data = output.to(lora_data.dtype).to(lora_data.device) - lora_data
                self.get_base_layer().weight = bnb.nn.Int8Params(
                    w_data.to("cpu"), requires_grad=False, has_fp16_weights=weight.has_fp16_weights
                ).to(weight.device)
                state.reset_grads()

        def get_delta_weight(self, adapter):
            return (
                transpose(
                    self.lora_B[adapter].weight @ self.lora_A[adapter].weight * self.mask,
                    False,
                )
                * self.scaling[adapter]
            )

        def forward(self, x: torch.Tensor, *args, **kwargs) -> torch.Tensor:
            if self.disable_adapters:
                if self.merged:
                    self.unmerge()
                result = self.base_layer(x, *args, **kwargs)
            elif self.merged:
                result = self.base_layer(x, *args, **kwargs)
            else:
                result = self.base_layer(x, *args, **kwargs)
                for active_adapter in self.active_adapters:
                    if active_adapter not in self.lora_A.keys():
                        continue
                    
                    # ##### sparse lora add
                    # weight = self.get_base_layer().weight
                    # state = self.get_base_layer().state
                    # if state.SCB is None:
                    #     state.SCB = weight.SCB

                    # # Dequantize the result of identity matrix and int8 weight because bitsandbytes does not support int8
                    # # dequantization directly
                    # im = torch.eye(weight.data.shape[-1]).contiguous().half().to(weight.device)
                    # im, imt, SCim, SCimt, coo_tensorim = bnb.functional.double_quant(im)
                    # im, Sim = bnb.functional.transform(im, "col32")
                    # if state.CxB is None:
                    #     state.CxB, state.SB = bnb.functional.transform(weight.data, to_order=state.formatB)
                    # out32, Sout32 = bnb.functional.igemmlt(im, state.CxB, Sim, state.SB)
                    # output = bnb.functional.mm_dequant(out32, Sout32, SCim, state.SCB, bias=None).t()

                    # w_data = output.float()

                    # mask = (w_data==0)

                    # # count = (w_data==0).sum().item()
                    # # total_params = w_data.numel()
                    # # print(f"layer sparsity {float(count)/total_params:.4f}")
                    # # print(mask)
                    # # import jbhvg

                    # ##### sparse lora add

                    lora_A = self.lora_A[active_adapter]
                    lora_B = self.lora_B[active_adapter]
                    dropout = self.lora_dropout[active_adapter]
                    scaling = self.scaling[active_adapter]

                    requires_conversion = not torch.is_autocast_enabled()
                    if requires_conversion:
                        expected_dtype = result.dtype
                        compute_dtype = lora_A.weight.dtype
                        if x.dtype != compute_dtype:
                            x = x.to(compute_dtype)
                    dropout_x = dropout(x)
                    # print(dropout_x.shape)
                    # print(lora_A.weight)
                    # print(lora_B.weight)
                    # # # print(lora_A(dropout_x))
                    # print(lora_B(lora_A(dropout_x)))
                    # # # print(dropout_x@lora_A.weight.T)
                    # # # print(dropout_x@lora_A.weight.T@lora_B.weight.T)
                    # print(dropout_x@(lora_B.weight@lora_A.weight).T)
                    # print("xxxxxxxx")
                    # # import xkbhvv
                    # a = torch.randn([5120,5120]).cuda()
                    # print(a)
                    # print(self.mask)
                    # print(a*self.mask)
                    # import jbyf

                    # output = lora_B(lora_A(dropout_x))
                    output = dropout_x@(lora_B.weight@lora_A.weight*self.mask).T
                    if requires_conversion:
                        output = output.to(expected_dtype)
                    output = output * scaling
                    result += output

            return result

        def __repr__(self) -> str:
            rep = super().__repr__()
            return "lora." + rep

    def dispatch_bnb_8bit(target: torch.nn.Module, adapter_name: str, **kwargs):
        new_module = None

        if isinstance(target, BaseTunerLayer):
            target_base_layer = target.get_base_layer()
        else:
            target_base_layer = target

        loaded_in_8bit = kwargs.get("loaded_in_8bit", False)
        if loaded_in_8bit and isinstance(target_base_layer, bnb.nn.Linear8bitLt):
            eightbit_kwargs = kwargs.copy()
            eightbit_kwargs.update(
                {
                    "has_fp16_weights": target.state.has_fp16_weights,
                    "memory_efficient_backward": target.state.memory_efficient_backward,
                    "threshold": target.state.threshold,
                    "index": target.index,
                }
            )
            new_module = Linear8bitLt(target, adapter_name, **eightbit_kwargs)

        return new_module


if is_bnb_4bit_available():

    class Linear4bit(torch.nn.Module, LoraLayer):
        # Lora implemented in a dense layer
        def __init__(
            self,
            base_layer: torch.nn.Module,
            adapter_name: str,
            r: int = 0,
            lora_alpha: int = 1,
            lora_dropout: float = 0.0,
            init_lora_weights: bool = True,
            use_rslora: bool = False,
            **kwargs,
        ) -> None:
            super().__init__()
            LoraLayer.__init__(self, base_layer)

            weight = self.get_base_layer().weight
            w_data = bnb.functional.dequantize_4bit(weight.data, weight.quant_state)
            count = (w_data==0).sum().item()
            total_params = w_data.numel()
            # print(f"layer sparsity {float(count)/total_params:.4f}")
            # import ccc

            self._active_adapter = adapter_name
            self.update_layer(adapter_name, r, lora_alpha, lora_dropout, init_lora_weights, use_rslora)

        def merge(self, safe_merge: bool = False, adapter_names: Optional[List[str]] = None) -> None:
            """
            Merge the active adapter weights into the base weights

            Args:
                safe_merge (`bool`, *optional*):
                    If True, the merge operation will be performed in a copy of the original weights and check for NaNs
                    before merging the weights. This is useful if you want to check if the merge operation will produce
                    NaNs. Defaults to `False`.
                adapter_names (`List[str]`, *optional*):
                    The list of adapter names that should be merged. If None, all active adapters will be merged.
                    Defaults to `None`.
            """
            if self.merged:
                warnings.warn(
                    f"Already following adapters were merged {','.join(self.merged_adapters)}. "
                    f"You are now additionally merging {','.join(self.active_adapters)}."
                )

            if adapter_names is None:
                adapter_names = self.active_adapters

            for active_adapter in adapter_names:
                if active_adapter not in self.lora_A.keys():
                    continue
                warnings.warn(
                    "Merge lora module to 4-bit linear may get different generations due to rounding errors."
                )
                # Refer to https://gist.github.com/ChrisHayduk/1a53463331f52dca205e55982baf9930
                weight = self.get_base_layer().weight
                kwargs = weight.__dict__
                lora_data = self.get_delta_weight(active_adapter)

                w_data = bnb.functional.dequantize_4bit(weight.data, weight.quant_state) + lora_data
                if safe_merge and not torch.isfinite(w_data).all():
                    raise ValueError(
                        f"NaNs detected in the merged weights. The adapter {active_adapter} seems to be broken"
                    )
                if "bnb_quantized" in kwargs:
                    kwargs["bnb_quantized"] = False
                self.get_base_layer().weight = bnb.nn.Params4bit(w_data.to("cpu"), requires_grad=False, **kwargs).to(
                    weight.device
                )
                self.merged_adapters.append(active_adapter)

        def unmerge(self) -> None:
            """
            This method unmerges all merged adapter layers from the base weights.
            """
            if not self.merged:
                warnings.warn("Already unmerged. Nothing to do.")
                return

            while len(self.merged_adapters) > 0:
                active_adapter = self.merged_adapters.pop()
                if active_adapter not in self.lora_A.keys():
                    continue
                warnings.warn(
                    "Unmerge lora module to 4-bit linear may get different generations due to rounding errors."
                )
                weight = self.get_base_layer().weight
                kwargs = weight.__dict__
                lora_data = self.get_delta_weight(active_adapter)
                w_data = bnb.functional.dequantize_4bit(weight.data, weight.quant_state) - lora_data
                if "bnb_quantized" in kwargs:
                    kwargs["bnb_quantized"] = False
                self.get_base_layer().weight = bnb.nn.Params4bit(w_data.to("cpu"), requires_grad=False, **kwargs).to(
                    weight.device
                )

        def get_delta_weight(self, adapter):
            return (
                transpose(
                    self.lora_B[adapter].weight @ self.lora_A[adapter].weight,
                    False,
                )
                * self.scaling[adapter]
            )

        def forward(self, x: torch.Tensor, *args, **kwargs) -> torch.Tensor:
            if self.disable_adapters:
                if self.merged:
                    self.unmerge()
                result = self.base_layer(x, *args, **kwargs)
            elif self.merged:
                result = self.base_layer(x, *args, **kwargs)
            else:
                result = self.base_layer(x, *args, **kwargs)
                # As per Tim Dettmers, for 4bit, we need to defensively clone here.
                # The reason is that in some cases, an error can occur that backprop
                # does not work on a manipulated view. This issue may be solved with
                # newer PyTorch versions but this would need extensive testing to be
                # sure.

                result = result.clone()

                for active_adapter in self.active_adapters:
                    if active_adapter not in self.lora_A.keys():
                        continue
                    lora_A = self.lora_A[active_adapter]
                    lora_B = self.lora_B[active_adapter]
                    dropout = self.lora_dropout[active_adapter]
                    scaling = self.scaling[active_adapter]

                    requires_conversion = not torch.is_autocast_enabled()
                    if requires_conversion:
                        expected_dtype = result.dtype
                        x = x.to(lora_A.weight.dtype)

                    output = lora_B(lora_A(dropout(x)))
                    if requires_conversion:
                        output = output.to(expected_dtype)
                    output = output * scaling
                    result += output
            # import jbhv

            return result

        def __repr__(self) -> str:
            rep = super().__repr__()
            return "lora." + rep

    def dispatch_bnb_4bit(target: torch.nn.Module, adapter_name: str, **kwargs):
        new_module = None

        if isinstance(target, BaseTunerLayer):
            target_base_layer = target.get_base_layer()
        else:
            target_base_layer = target

        loaded_in_4bit = kwargs.get("loaded_in_4bit", False)
        if loaded_in_4bit and is_bnb_4bit_available() and isinstance(target_base_layer, bnb.nn.Linear4bit):
            fourbit_kwargs = kwargs.copy()
            fourbit_kwargs.update(
                {
                    "compute_dtype": target_base_layer.compute_dtype,
                    "compress_statistics": target_base_layer.weight.compress_statistics,
                    "quant_type": target_base_layer.weight.quant_type,
                }
            )
            new_module = Linear4bit(target, adapter_name, **fourbit_kwargs)

        return new_module
