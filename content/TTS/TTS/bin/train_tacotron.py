#!/usr/bin/env python3
"""Trains Tacotron based TTS models."""

import os
import sys
import time
import traceback
from random import randrange

import numpy as np
import torch
from torch.utils.data import DataLoader

from TTS.tts.datasets.preprocess import load_meta_data
from TTS.tts.datasets.TTSDataset import MyDataset
from TTS.tts.layers.losses import TacotronLoss
from TTS.tts.utils.generic_utils import setup_model
from TTS.tts.utils.io import save_best_model, save_checkpoint
from TTS.tts.utils.measures import alignment_diagonal_score
from TTS.tts.utils.speakers import parse_speakers
from TTS.tts.utils.synthesis import synthesis
from TTS.tts.utils.text.symbols import make_symbols, phonemes, symbols
from TTS.tts.utils.visual import plot_alignment, plot_spectrogram
from TTS.utils.arguments import init_training
from TTS.utils.audio import AudioProcessor
from TTS.utils.distribute import DistributedSampler, apply_gradient_allreduce, init_distributed, reduce_tensor
from TTS.utils.generic_utils import KeepAverage, count_parameters, remove_experiment_folder, set_init_dict
from TTS.utils.radam import RAdam
from TTS.utils.training import (
    NoamLR,
    adam_weight_decay,
    check_update,
    gradual_training_scheduler,
    set_weight_decay,
    setup_torch_training_env,
)

use_cuda, num_gpus = setup_torch_training_env(True, False)


def setup_loader(ap, r, is_val=False, verbose=False, dataset=None):
    if is_val and not config.run_eval:
        loader = None
    else:
        if dataset is None:
            dataset = MyDataset(
                r,
                config.text_cleaner,
                compute_linear_spec=config.model.lower() == "tacotron",
                meta_data=meta_data_eval if is_val else meta_data_train,
                ap=ap,
                tp=config.characters,
                add_blank=config["add_blank"],
                batch_group_size=0 if is_val else config.batch_group_size * config.batch_size,
                min_seq_len=config.min_seq_len,
                max_seq_len=config.max_seq_len,
                phoneme_cache_path=config.phoneme_cache_path,
                use_phonemes=config.use_phonemes,
                phoneme_language=config.phoneme_language,
                enable_eos_bos=config.enable_eos_bos_chars,
                verbose=verbose,
                speaker_mapping=(
                    speaker_mapping
                    if (config.use_speaker_embedding and config.use_external_speaker_embedding_file)
                    else None
                ),
            )

            if config.use_phonemes and config.compute_input_seq_cache:
                # precompute phonemes to have a better estimate of sequence lengths.
                dataset.compute_input_seq(config.num_loader_workers)
            dataset.sort_items()

        sampler = DistributedSampler(dataset) if num_gpus > 1 else None
        loader = DataLoader(
            dataset,
            batch_size=config.eval_batch_size if is_val else config.batch_size,
            shuffle=False,
            collate_fn=dataset.collate_fn,
            drop_last=False,
            sampler=sampler,
            num_workers=config.num_val_loader_workers if is_val else config.num_loader_workers,
            pin_memory=False,
        )
    return loader


def format_data(data):
    # setup input data
    text_input = data[0]
    text_lengths = data[1]
    speaker_names = data[2]
    linear_input = data[3] if config.model.lower() in ["tacotron"] else None
    mel_input = data[4]
    mel_lengths = data[5]
    stop_targets = data[6]
    max_text_length = torch.max(text_lengths.float())
    max_spec_length = torch.max(mel_lengths.float())

    if config.use_speaker_embedding:
        if config.use_external_speaker_embedding_file:
            speaker_embeddings = data[8]
            speaker_ids = None
        else:
            speaker_ids = [speaker_mapping[speaker_name] for speaker_name in speaker_names]
            speaker_ids = torch.LongTensor(speaker_ids)
            speaker_embeddings = None
    else:
        speaker_embeddings = None
        speaker_ids = None

    # set stop targets view, we predict a single stop token per iteration.
    stop_targets = stop_targets.view(text_input.shape[0], stop_targets.size(1) // config.r, -1)
    stop_targets = (stop_targets.sum(2) > 0.0).unsqueeze(2).float().squeeze(2)

    # dispatch data to GPU
    if use_cuda:
        text_input = text_input.cuda(non_blocking=True)
        text_lengths = text_lengths.cuda(non_blocking=True)
        mel_input = mel_input.cuda(non_blocking=True)
        mel_lengths = mel_lengths.cuda(non_blocking=True)
        linear_input = linear_input.cuda(non_blocking=True) if config.model.lower() in ["tacotron"] else None
        stop_targets = stop_targets.cuda(non_blocking=True)
        if speaker_ids is not None:
            speaker_ids = speaker_ids.cuda(non_blocking=True)
        if speaker_embeddings is not None:
            speaker_embeddings = speaker_embeddings.cuda(non_blocking=True)

    return (
        text_input,
        text_lengths,
        mel_input,
        mel_lengths,
        linear_input,
        stop_targets,
        speaker_ids,
        speaker_embeddings,
        max_text_length,
        max_spec_length,
    )


def train(data_loader, model, criterion, optimizer, optimizer_st, scheduler, ap, global_step, epoch, scaler, scaler_st):
    model.train()
    epoch_time = 0
    keep_avg = KeepAverage()
    if use_cuda:
        batch_n_iter = int(len(data_loader.dataset) / (config.batch_size * num_gpus))
    else:
        batch_n_iter = int(len(data_loader.dataset) / config.batch_size)
    end_time = time.time()
    c_logger.print_train_start()
    for num_iter, data in enumerate(data_loader):
        start_time = time.time()

        # format data
        (
            text_input,
            text_lengths,
            mel_input,
            mel_lengths,
            linear_input,
            stop_targets,
            speaker_ids,
            speaker_embeddings,
            max_text_length,
            max_spec_length,
        ) = format_data(data)
        loader_time = time.time() - end_time

        global_step += 1

        # setup lr
        if config.noam_schedule:
            scheduler.step()

        optimizer.zero_grad()
        if optimizer_st:
            optimizer_st.zero_grad()

        with torch.cuda.amp.autocast(enabled=config.mixed_precision):
            # forward pass model
            if config.bidirectional_decoder or config.double_decoder_consistency:
                (
                    decoder_output,
                    postnet_output,
                    alignments,
                    stop_tokens,
                    decoder_backward_output,
                    alignments_backward,
                ) = model(
                    text_input,
                    text_lengths,
                    mel_input,
                    mel_lengths,
                    speaker_ids=speaker_ids,
                    speaker_embeddings=speaker_embeddings,
                )
            else:
                decoder_output, postnet_output, alignments, stop_tokens = model(
                    text_input,
                    text_lengths,
                    mel_input,
                    mel_lengths,
                    speaker_ids=speaker_ids,
                    speaker_embeddings=speaker_embeddings,
                )
                decoder_backward_output = None
                alignments_backward = None

            # set the [alignment] lengths wrt reduction factor for guided attention
            if mel_lengths.max() % model.decoder.r != 0:
                alignment_lengths = (
                    mel_lengths + (model.decoder.r - (mel_lengths.max() % model.decoder.r))
                ) // model.decoder.r
            else:
                alignment_lengths = mel_lengths // model.decoder.r

            # compute loss
            loss_dict = criterion(
                postnet_output,
                decoder_output,
                mel_input,
                linear_input,
                stop_tokens,
                stop_targets,
                mel_lengths,
                decoder_backward_output,
                alignments,
                alignment_lengths,
                alignments_backward,
                text_lengths,
            )

        # check nan loss
        if torch.isnan(loss_dict["loss"]).any():
            raise RuntimeError(f"Detected NaN loss at step {global_step}.")

        # optimizer step
        if config.mixed_precision:
            # model optimizer step in mixed precision mode
            scaler.scale(loss_dict["loss"]).backward()
            scaler.unscale_(optimizer)
            optimizer, current_lr = adam_weight_decay(optimizer)
            grad_norm, _ = check_update(model, config.grad_clip, ignore_stopnet=True)
            scaler.step(optimizer)
            scaler.update()

            # stopnet optimizer step
            if config.separate_stopnet:
                scaler_st.scale(loss_dict["stopnet_loss"]).backward()
                scaler.unscale_(optimizer_st)
                optimizer_st, _ = adam_weight_decay(optimizer_st)
                grad_norm_st, _ = check_update(model.decoder.stopnet, 1.0)
                scaler_st.step(optimizer)
                scaler_st.update()
            else:
                grad_norm_st = 0
        else:
            # main model optimizer step
            loss_dict["loss"].backward()
            optimizer, current_lr = adam_weight_decay(optimizer)
            grad_norm, _ = check_update(model, config.grad_clip, ignore_stopnet=True)
            optimizer.step()

            # stopnet optimizer step
            if config.separate_stopnet:
                loss_dict["stopnet_loss"].backward()
                optimizer_st, _ = adam_weight_decay(optimizer_st)
                grad_norm_st, _ = check_update(model.decoder.stopnet, 1.0)
                optimizer_st.step()
            else:
                grad_norm_st = 0

        # compute alignment error (the lower the better )
        align_error = 1 - alignment_diagonal_score(alignments)
        loss_dict["align_error"] = align_error

        step_time = time.time() - start_time
        epoch_time += step_time

        # aggregate losses from processes
        if num_gpus > 1:
            loss_dict["postnet_loss"] = reduce_tensor(loss_dict["postnet_loss"].data, num_gpus)
            loss_dict["decoder_loss"] = reduce_tensor(loss_dict["decoder_loss"].data, num_gpus)
            loss_dict["loss"] = reduce_tensor(loss_dict["loss"].data, num_gpus)
            loss_dict["stopnet_loss"] = (
                reduce_tensor(loss_dict["stopnet_loss"].data, num_gpus) if config.stopnet else loss_dict["stopnet_loss"]
            )

        # detach loss values
        loss_dict_new = dict()
        for key, value in loss_dict.items():
            if isinstance(value, (int, float)):
                loss_dict_new[key] = value
            else:
                loss_dict_new[key] = value.item()
        loss_dict = loss_dict_new

        # update avg stats
        update_train_values = dict()
        for key, value in loss_dict.items():
            update_train_values["avg_" + key] = value
        update_train_values["avg_loader_time"] = loader_time
        update_train_values["avg_step_time"] = step_time
        keep_avg.update_values(update_train_values)

        # print training progress
        if global_step % config.print_step == 0:
            log_dict = {
                "max_spec_length": [max_spec_length, 1],  # value, precision
                "max_text_length": [max_text_length, 1],
                "step_time": [step_time, 4],
                "loader_time": [loader_time, 2],
                "current_lr": current_lr,
            }
            c_logger.print_train_step(batch_n_iter, num_iter, global_step, log_dict, loss_dict, keep_avg.avg_values)

        if args.rank == 0:
            # Plot Training Iter Stats
            # reduce TB load
            if global_step % config.tb_plot_step == 0:
                iter_stats = {
                    "lr": current_lr,
                    "grad_norm": grad_norm,
                    "grad_norm_st": grad_norm_st,
                    "step_time": step_time,
                }
                iter_stats.update(loss_dict)
                tb_logger.tb_train_iter_stats(global_step, iter_stats)

            if global_step % config.save_step == 0:
                if config.checkpoint:
                    # save model
                    save_checkpoint(
                        model,
                        optimizer,
                        global_step,
                        epoch,
                        model.decoder.r,
                        OUT_PATH,
                        optimizer_st=optimizer_st,
                        model_loss=loss_dict["postnet_loss"],
                        characters=model_characters,
                        scaler=scaler.state_dict() if config.mixed_precision else None,
                    )

                # Diagnostic visualizations
                const_spec = postnet_output[0].data.cpu().numpy()
                gt_spec = (
                    linear_input[0].data.cpu().numpy()
                    if config.model in ["Tacotron", "TacotronGST"]
                    else mel_input[0].data.cpu().numpy()
                )
                align_img = alignments[0].data.cpu().numpy()

                figures = {
                    "prediction": plot_spectrogram(const_spec, ap, output_fig=False),
                    "ground_truth": plot_spectrogram(gt_spec, ap, output_fig=False),
                    "alignment": plot_alignment(align_img, output_fig=False),
                }

                if config.bidirectional_decoder or config.double_decoder_consistency:
                    figures["alignment_backward"] = plot_alignment(
                        alignments_backward[0].data.cpu().numpy(), output_fig=False
                    )

                tb_logger.tb_train_figures(global_step, figures)

                # Sample audio
                if config.model in ["Tacotron", "TacotronGST"]:
                    train_audio = ap.inv_spectrogram(const_spec.T)
                else:
                    train_audio = ap.inv_melspectrogram(const_spec.T)
                tb_logger.tb_train_audios(global_step, {"TrainAudio": train_audio}, config.audio["sample_rate"])
        end_time = time.time()

    # print epoch stats
    c_logger.print_train_epoch_end(global_step, epoch, epoch_time, keep_avg)

    # Plot Epoch Stats
    if args.rank == 0:
        epoch_stats = {"epoch_time": epoch_time}
        epoch_stats.update(keep_avg.avg_values)
        tb_logger.tb_train_epoch_stats(global_step, epoch_stats)
        if config.tb_model_param_stats:
            tb_logger.tb_model_weights(model, global_step)
    return keep_avg.avg_values, global_step


@torch.no_grad()
def evaluate(data_loader, model, criterion, ap, global_step, epoch):
    model.eval()
    epoch_time = 0
    keep_avg = KeepAverage()
    c_logger.print_eval_start()
    if data_loader is not None:
        for num_iter, data in enumerate(data_loader):
            start_time = time.time()

            # format data
            (
                text_input,
                text_lengths,
                mel_input,
                mel_lengths,
                linear_input,
                stop_targets,
                speaker_ids,
                speaker_embeddings,
                _,
                _,
            ) = format_data(data)
            assert mel_input.shape[1] % model.decoder.r == 0

            # forward pass model
            if config.bidirectional_decoder or config.double_decoder_consistency:
                (
                    decoder_output,
                    postnet_output,
                    alignments,
                    stop_tokens,
                    decoder_backward_output,
                    alignments_backward,
                ) = model(
                    text_input, text_lengths, mel_input, speaker_ids=speaker_ids, speaker_embeddings=speaker_embeddings
                )
            else:
                decoder_output, postnet_output, alignments, stop_tokens = model(
                    text_input, text_lengths, mel_input, speaker_ids=speaker_ids, speaker_embeddings=speaker_embeddings
                )
                decoder_backward_output = None
                alignments_backward = None

            # set the alignment lengths wrt reduction factor for guided attention
            if mel_lengths.max() % model.decoder.r != 0:
                alignment_lengths = (
                    mel_lengths + (model.decoder.r - (mel_lengths.max() % model.decoder.r))
                ) // model.decoder.r
            else:
                alignment_lengths = mel_lengths // model.decoder.r

            # compute loss
            loss_dict = criterion(
                postnet_output,
                decoder_output,
                mel_input,
                linear_input,
                stop_tokens,
                stop_targets,
                mel_lengths,
                decoder_backward_output,
                alignments,
                alignment_lengths,
                alignments_backward,
                text_lengths,
            )

            # step time
            step_time = time.time() - start_time
            epoch_time += step_time

            # compute alignment score
            align_error = 1 - alignment_diagonal_score(alignments)
            loss_dict["align_error"] = align_error

            # aggregate losses from processes
            if num_gpus > 1:
                loss_dict["postnet_loss"] = reduce_tensor(loss_dict["postnet_loss"].data, num_gpus)
                loss_dict["decoder_loss"] = reduce_tensor(loss_dict["decoder_loss"].data, num_gpus)
                if config.stopnet:
                    loss_dict["stopnet_loss"] = reduce_tensor(loss_dict["stopnet_loss"].data, num_gpus)

            # detach loss values
            loss_dict_new = dict()
            for key, value in loss_dict.items():
                if isinstance(value, (int, float)):
                    loss_dict_new[key] = value
                else:
                    loss_dict_new[key] = value.item()
            loss_dict = loss_dict_new

            # update avg stats
            update_train_values = dict()
            for key, value in loss_dict.items():
                update_train_values["avg_" + key] = value
            keep_avg.update_values(update_train_values)

            if config.print_eval:
                c_logger.print_eval_step(num_iter, loss_dict, keep_avg.avg_values)

        if args.rank == 0:
            # Diagnostic visualizations
            idx = np.random.randint(mel_input.shape[0])
            const_spec = postnet_output[idx].data.cpu().numpy()
            gt_spec = (
                linear_input[idx].data.cpu().numpy()
                if config.model in ["Tacotron", "TacotronGST"]
                else mel_input[idx].data.cpu().numpy()
            )
            align_img = alignments[idx].data.cpu().numpy()

            eval_figures = {
                "prediction": plot_spectrogram(const_spec, ap, output_fig=False),
                "ground_truth": plot_spectrogram(gt_spec, ap, output_fig=False),
                "alignment": plot_alignment(align_img, output_fig=False),
            }

            # Sample audio
            if config.model.lower() in ["tacotron"]:
                eval_audio = ap.inv_spectrogram(const_spec.T)
            else:
                eval_audio = ap.inv_melspectrogram(const_spec.T)
            tb_logger.tb_eval_audios(global_step, {"ValAudio": eval_audio}, config.audio["sample_rate"])

            # Plot Validation Stats

            if config.bidirectional_decoder or config.double_decoder_consistency:
                align_b_img = alignments_backward[idx].data.cpu().numpy()
                eval_figures["alignment2"] = plot_alignment(align_b_img, output_fig=False)
            tb_logger.tb_eval_stats(global_step, keep_avg.avg_values)
            tb_logger.tb_eval_figures(global_step, eval_figures)

    if args.rank == 0 and epoch > config.test_delay_epochs:
        if config.test_sentences_file:
            with open(config.test_sentences_file, "r") as f:
                test_sentences = [s.strip() for s in f.readlines()]
        else:
            test_sentences = [
                "It took me quite a long time to develop a voice, and now that I have it I'm not going to be silent.",
                "Be a voice, not an echo.",
                "I'm sorry Dave. I'm afraid I can't do that.",
                "This cake is great. It's so delicious and moist.",
                "Prior to November 22, 1963.",
            ]

        # test sentences
        test_audios = {}
        test_figures = {}
        print(" | > Synthesizing test sentences")
        speaker_id = 0 if config.use_speaker_embedding else None
        speaker_embedding = (
            speaker_mapping[list(speaker_mapping.keys())[randrange(len(speaker_mapping) - 1)]]["embedding"]
            if config.use_external_speaker_embedding_file and config.use_speaker_embedding
            else None
        )
        style_wav = config.gst_style_input
        if style_wav is None and config.gst is not None:
            # inicialize GST with zero dict.
            style_wav = {}
            print("WARNING: You don't provided a gst style wav, for this reason we use a zero tensor!")
            for i in range(config.gst["gst_num_style_tokens"]):
                style_wav[str(i)] = 0
        for idx, test_sentence in enumerate(test_sentences):
            try:
                wav, alignment, decoder_output, postnet_output, stop_tokens, _ = synthesis(
                    model,
                    test_sentence,
                    config,
                    use_cuda,
                    ap,
                    speaker_id=speaker_id,
                    speaker_embedding=speaker_embedding,
                    style_wav=style_wav,
                    truncated=False,
                    enable_eos_bos_chars=config.enable_eos_bos_chars,  # pylint: disable=unused-argument
                    use_griffin_lim=True,
                    do_trim_silence=False,
                )

                file_path = os.path.join(AUDIO_PATH, str(global_step))
                os.makedirs(file_path, exist_ok=True)
                file_path = os.path.join(file_path, "TestSentence_{}.wav".format(idx))
                ap.save_wav(wav, file_path)
                test_audios["{}-audio".format(idx)] = wav
                test_figures["{}-prediction".format(idx)] = plot_spectrogram(postnet_output, ap, output_fig=False)
                test_figures["{}-alignment".format(idx)] = plot_alignment(alignment, output_fig=False)
            except:  # pylint: disable=bare-except
                print(" !! Error creating Test Sentence -", idx)
                traceback.print_exc()
        tb_logger.tb_test_audios(global_step, test_audios, config.audio["sample_rate"])
        tb_logger.tb_test_figures(global_step, test_figures)
    return keep_avg.avg_values


def main(args):  # pylint: disable=redefined-outer-name
    # pylint: disable=global-variable-undefined
    global meta_data_train, meta_data_eval, speaker_mapping, symbols, phonemes, model_characters
    # Audio processor
    ap = AudioProcessor(**config.audio.to_dict())

    # setup custom characters if set in config file.
    if config.characters is not None:
        symbols, phonemes = make_symbols(**config.characters.to_dict())

    # DISTRUBUTED
    if num_gpus > 1:
        init_distributed(args.rank, num_gpus, args.group_id, config.distributed["backend"], config.distributed["url"])
    num_chars = len(phonemes) if config.use_phonemes else len(symbols)
    model_characters = phonemes if config.use_phonemes else symbols

    # load data instances
    meta_data_train, meta_data_eval = load_meta_data(config.datasets)

    # set the portion of the data used for training
    if config.has("train_portion"):
        meta_data_train = meta_data_train[: int(len(meta_data_train) * config.train_portion)]
    if config.has("eval_portion"):
        meta_data_eval = meta_data_eval[: int(len(meta_data_eval) * config.eval_portion)]

    # parse speakers
    num_speakers, speaker_embedding_dim, speaker_mapping = parse_speakers(config, args, meta_data_train, OUT_PATH)

    model = setup_model(num_chars, num_speakers, config, speaker_embedding_dim)

    # scalers for mixed precision training
    scaler = torch.cuda.amp.GradScaler() if config.mixed_precision else None
    scaler_st = torch.cuda.amp.GradScaler() if config.mixed_precision and config.separate_stopnet else None

    params = set_weight_decay(model, config.wd)
    optimizer = RAdam(params, lr=config.lr, weight_decay=0)
    if config.stopnet and config.separate_stopnet:
        optimizer_st = RAdam(model.decoder.stopnet.parameters(), lr=config.lr, weight_decay=0)
    else:
        optimizer_st = None

    # setup criterion
    criterion = TacotronLoss(config, stopnet_pos_weight=config.stopnet_pos_weight, ga_sigma=0.4)
    if args.restore_path:
        print(f" > Restoring from {os.path.basename(args.restore_path)}...")
        checkpoint = torch.load(args.restore_path, map_location="cpu")
        try:
            print(" > Restoring Model...")
            model.load_state_dict(checkpoint["model"])
            # optimizer restore
            print(" > Restoring Optimizer...")
            optimizer.load_state_dict(checkpoint["optimizer"])
            if "scaler" in checkpoint and config.mixed_precision:
                print(" > Restoring AMP Scaler...")
                scaler.load_state_dict(checkpoint["scaler"])
        except (KeyError, RuntimeError):
            print(" > Partial model initialization...")
            model_dict = model.state_dict()
            model_dict = set_init_dict(model_dict, checkpoint["model"], config)
            model.load_state_dict(model_dict)
            del model_dict

        for group in optimizer.param_groups:
            group["lr"] = config.lr
        print(" > Model restored from step %d" % checkpoint["step"], flush=True)
        args.restore_step = checkpoint["step"]
    else:
        args.restore_step = 0

    if use_cuda:
        model.cuda()
        criterion.cuda()

    # DISTRUBUTED
    if num_gpus > 1:
        model = apply_gradient_allreduce(model)

    if config.noam_schedule:
        scheduler = NoamLR(optimizer, warmup_steps=config.warmup_steps, last_epoch=args.restore_step - 1)
    else:
        scheduler = None

    num_params = count_parameters(model)
    print("\n > Model has {} parameters".format(num_params), flush=True)

    if args.restore_step == 0 or not args.best_path:
        best_loss = float("inf")
        print(" > Starting with inf best loss.")
    else:
        print(" > Restoring best loss from " f"{os.path.basename(args.best_path)} ...")
        best_loss = torch.load(args.best_path, map_location="cpu")["model_loss"]
        print(f" > Starting with loaded last best loss {best_loss}.")
    keep_all_best = config.keep_all_best
    keep_after = config.keep_after  # void if keep_all_best False

    # define data loaders
    train_loader = setup_loader(ap, model.decoder.r, is_val=False, verbose=True)
    eval_loader = setup_loader(ap, model.decoder.r, is_val=True)

    global_step = args.restore_step
    for epoch in range(0, config.epochs):
        c_logger.print_epoch_start(epoch, config.epochs)
        # set gradual training
        if config.gradual_training is not None:
            r, config.batch_size = gradual_training_scheduler(global_step, config)
            config.r = r
            model.decoder.set_r(r)
            if config.bidirectional_decoder:
                model.decoder_backward.set_r(r)
            train_loader.dataset.outputs_per_step = r
            eval_loader.dataset.outputs_per_step = r
            train_loader = setup_loader(ap, model.decoder.r, is_val=False, dataset=train_loader.dataset)
            eval_loader = setup_loader(ap, model.decoder.r, is_val=True, dataset=eval_loader.dataset)
            print("\n > Number of output frames:", model.decoder.r)
        # train one epoch
        train_avg_loss_dict, global_step = train(
            train_loader,
            model,
            criterion,
            optimizer,
            optimizer_st,
            scheduler,
            ap,
            global_step,
            epoch,
            scaler,
            scaler_st,
        )
        # eval one epoch
        eval_avg_loss_dict = evaluate(eval_loader, model, criterion, ap, global_step, epoch)
        c_logger.print_epoch_end(epoch, eval_avg_loss_dict)
        target_loss = train_avg_loss_dict["avg_postnet_loss"]
        if config.run_eval:
            target_loss = eval_avg_loss_dict["avg_postnet_loss"]
        best_loss = save_best_model(
            target_loss,
            best_loss,
            model,
            optimizer,
            global_step,
            epoch,
            config.r,
            OUT_PATH,
            model_characters,
            keep_all_best=keep_all_best,
            keep_after=keep_after,
            scaler=scaler.state_dict() if config.mixed_precision else None,
        )


if __name__ == "__main__":
    args, config, OUT_PATH, AUDIO_PATH, c_logger, tb_logger = init_training(sys.argv)
    try:
        main(args)
    except KeyboardInterrupt:
        remove_experiment_folder(OUT_PATH)
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-except
        remove_experiment_folder(OUT_PATH)
        traceback.print_exc()
        sys.exit(1)
