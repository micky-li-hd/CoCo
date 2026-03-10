# 🎩 CoCo: Code as CoT for Text-to-Image Preview and Rare Concept Generation

Official repository for the paper "[CoCo: CoCo as CoT for Text-to-Image Preview and Rare Concept Generation](https://arxiv.org/abs/2603.08652)".

[[📖 Paper](https://arxiv.org/abs/2603.08652)] [[🤗 Model](https://huggingface.co/mickyhimself/CoCo)]

<p align="center">
    <img src="figs/pipeline.png" width="100%"> <br>
</p>

## 💥 News
- **[2026.3.09]** We release the [arxiv paper](https://arxiv.org/abs/2603.08652). Code is coming soon. 🔥

## 🪄 Draft Before Generation

<p align="center">
    <img src="figs/teaser_mk1.png" width="100%"> <br>
</p>

We propose **CoCo-as-CoT _(CoCo)_**, a novel interleaved reasoning paradigm that fully leverages both textual and visual contents in CoT for better planning and verification. 

Our method 🎨 **first generates Python MatPlotlib Code and render by sandbox as a preview**, providing more concrete and structural visual planning and guidance. 

Then, we 🔎 **employ the model’s inherent understanding capability to verify potential semantic misalignments** between the draft and input prompt, and 🖼️ **achieve higher fidelity and stronger semantic alignment**.



## 🧠 Related Work

Explore our additional research on **Autoregressive Text-to-Image Generation** and  **CoT Reasoning** 

- **[GGEBench]** [GEBench: Benchmarking Image Generation Models as GUI Environments](https://arxiv.org/pdf/2602.09007)
- **[VIBE]** [VIBE: A Systematic Benchmark for Visual Instruction-Driven Image Editing](https://arxiv.org/abs/2602.01851)
- **[GENIUS]** [GENIUS: Generative Fluid Intelligence Evaluation Suite](https://arxiv.org/pdf/2602.11144)
- **[DraCo]** [DraCo: Draft as CoT for Text-to-Image Preview and Rare Concept Generation](https://arxiv.org/pdf/2512.05112)
- **[T2I-R1]** [T2I-R1: Reinforcing Image Generation with Collaborative Semantic-level and Token-level CoT](https://arxiv.org/pdf/2505.00703)
- **[ULMEvalKit]** [ULMEvalKit: One-Stop Eval ToolKit for Image Generation](https://github.com/ULMEvalKit/ULMEvalKit)
- **[Echo-4o]** [Echo-4o: Harnessing the Power of GPT-4o Synthetic Images for Improved Image Generation](https://arxiv.org/pdf/2508.09987)
- **[Image Generation CoT]** [Can We Generate Images with CoT? Let's Verify and Reinforce Image Generation Step by Step?](https://arxiv.org/pdf/2501.13926)
- **[Awesome-Nano-Banana-images]** [An Image Gallery Collecting Prompts to Create Stunning Images with Nano-banana](https://github.com/PicoTrex/Awesome-Nano-Banana-images)