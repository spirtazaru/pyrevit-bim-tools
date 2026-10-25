# -*- coding: utf-8 -*-
"""Общие окна и диалоги кнопок КЖ в стиле «Аудита модели».

Оранжевая палитра, верхняя панель с «точками» закрыть/свернуть/развернуть, левое меню по разделам,
свои модальные диалоги show_message вместо TaskDialog. Стили взяты из «Схем СС», чтобы все окна
пакета выглядели одинаково.

Окна КЖ модальные (ShowDialog): обработчики кнопок выполняются в контексте команды pyRevit,
поэтому транзакции можно запускать прямо из них, без ExternalEvent.
"""
import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import System
from System.Diagnostics import Process
from System.Windows import WindowState, Visibility
from System.Windows.Controls import Button
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Markup import XamlReader
from System.Windows.Media import VisualTreeHelper


# ==========================================================
# ДИАЛОГИ В СТИЛЕ ОКНА АУДИТА (вместо стандартных TaskDialog)
# ==========================================================
# Палитры: 'info' — оранжевая (результаты), 'warn' — красная (предупреждения и подтверждения)
DIALOG_PALETTES = {
    'info': {'__TOP__': '#FFFF9440', '__BOTTOM__': '#FFFF6A00', '__ACCENT__': '#FFFF7A1A',
             '__SOFT__': '#1AFF7A1A', '__BORDER__': '#66FF7A1A', '__TITLE__': '#FF2B2118'},
    'warn': {'__TOP__': '#FFFF6B4A', '__BOTTOM__': '#FFDC2F1C', '__ACCENT__': '#FFE8452C',
             '__SOFT__': '#1AE8452C', '__BORDER__': '#80E8452C', '__TITLE__': '#FFA8241A'},
}

XAML_DIALOG = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Width="580" SizeToContent="Height" WindowStyle="None" AllowsTransparency="True"
        Background="Transparent" ResizeMode="NoResize" ShowInTaskbar="False"
        FontFamily="Segoe UI" FontSize="14" Foreground="#FF2B2118">
    <Window.Resources>
        <LinearGradientBrush x:Key="AccentGrad" StartPoint="0,0" EndPoint="0,1">
            <GradientStop Color="__TOP__" Offset="0"/>
            <GradientStop Color="__BOTTOM__" Offset="1"/>
        </LinearGradientBrush>
        <Style x:Key="PrimaryButton" TargetType="Button">
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="MinWidth" Value="110"/>
            <Setter Property="Padding" Value="20,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Name="bd" Background="{StaticResource AccentGrad}" CornerRadius="19" Padding="{TemplateBinding Padding}">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="bd" Property="Opacity" Value="0.9"/></Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
        <Style x:Key="GlassButton" TargetType="Button">
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="MinWidth" Value="110"/>
            <Setter Property="Padding" Value="18,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="__BORDER__" BorderThickness="1" Padding="{TemplateBinding Padding}">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="#FFFFFFFF"/>
                                <Setter TargetName="bd" Property="BorderBrush" Value="__ACCENT__"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
        <Style TargetType="ScrollBar">
            <Setter Property="OverridesDefaultStyle" Value="True"/>
            <Setter Property="Width" Value="10"/>
            <Setter Property="Margin" Value="4,0,0,0"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="ScrollBar">
                        <Grid>
                            <Border Background="__SOFT__" CornerRadius="5"/>
                            <Track Name="PART_Track" IsDirectionReversed="True">
                                <Track.Thumb>
                                    <Thumb>
                                        <Thumb.Template>
                                            <ControlTemplate TargetType="Thumb">
                                                <Border Background="{StaticResource AccentGrad}" CornerRadius="5" Margin="1" Opacity="0.8"/>
                                            </ControlTemplate>
                                        </Thumb.Template>
                                    </Thumb>
                                </Track.Thumb>
                            </Track>
                        </Grid>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
    </Window.Resources>

    <Border Margin="16" CornerRadius="20" Background="#FAFFFFFF" BorderBrush="__BORDER__" BorderThickness="1.5">
        <Border.Effect>
            <DropShadowEffect BlurRadius="18" ShadowDepth="0" Opacity="0.3" Color="#FF000000"/>
        </Border.Effect>
        <StackPanel>
            <Border Name="HeaderBar" CornerRadius="19,19,0,0" Background="__SOFT__" Padding="20,14" Cursor="SizeAll">
                <StackPanel Orientation="Horizontal">
                    <Grid Width="30" Height="30" Margin="0,0,12,0">
                        <Ellipse Fill="{StaticResource AccentGrad}"/>
                        <TextBlock Name="tbIcon" Text="!" Foreground="White" FontWeight="Bold" FontSize="17"
                                   HorizontalAlignment="Center" VerticalAlignment="Center"/>
                    </Grid>
                    <TextBlock Name="tbTitle" Foreground="__TITLE__" FontWeight="Bold" FontSize="16" VerticalAlignment="Center"/>
                </StackPanel>
            </Border>
            <TextBlock Name="tbMessage" TextWrapping="Wrap" FontSize="15" FontWeight="SemiBold" Margin="22,16,22,0"/>
            <Border Name="bdInput" Margin="22,12,22,0" CornerRadius="19" Background="#FFFFFFFF"
                    BorderBrush="__BORDER__" BorderThickness="1" Padding="14,0" Height="38">
                <TextBox Name="tbInput" BorderThickness="0" Background="Transparent" VerticalAlignment="Center" FontSize="14"/>
            </Border>
            <Border Name="bdDetails" Margin="22,12,22,0" CornerRadius="12" Background="__SOFT__"
                    BorderBrush="__BORDER__" BorderThickness="1" Padding="14,10,8,10">
                <StackPanel>
                    <TextBlock Name="tbDetailsHeader" FontWeight="Bold" Foreground="__TITLE__" Margin="0,0,0,6"/>
                    <ScrollViewer MaxHeight="320" VerticalScrollBarVisibility="Auto">
                        <TextBlock Name="tbDetails" TextWrapping="Wrap" FontSize="13" LineHeight="20"/>
                    </ScrollViewer>
                </StackPanel>
            </Border>
            <StackPanel Orientation="Horizontal" HorizontalAlignment="Right" Margin="22,18,22,20">
                <Button Name="btnNo" Style="{StaticResource GlassButton}" Content="Нет" Margin="0,0,10,0" IsCancel="True"/>
                <Button Name="btnYes" Style="{StaticResource PrimaryButton}" Content="Да"/>
            </StackPanel>
        </StackPanel>
    </Border>
</Window>"""

# Окно плагина — владелец диалогов (задаётся при его создании), чтобы диалог был поверх и по центру
dialog_owner = None


def _run_dialog(text, kind, title, details, ask, limit, input_default, details_header):
    xaml = XAML_DIALOG
    for token, color in DIALOG_PALETTES.get(kind, DIALOG_PALETTES['warn']).items():
        xaml = xaml.replace(token, color)
    dlg = XamlReader.Parse(xaml)
    result = [False]

    tb_title = dlg.FindName('tbTitle')
    tb_title.Text = title or (u"Подтверждение" if ask else (u"Внимание" if kind == 'warn' else u"КЖ"))
    dlg.Title = tb_title.Text
    dlg.FindName('tbIcon').Text = u"!" if kind == 'warn' else u"i"
    dlg.FindName('tbMessage').Text = text

    tb_input = dlg.FindName('tbInput')
    if input_default is None:
        dlg.FindName('bdInput').Visibility = Visibility.Collapsed
    else:
        tb_input.Text = input_default

    if details:
        lines = list(details)
        shown = u"\n".join(lines[:limit])
        if len(lines) > limit:
            shown += u"\n… и ещё {} шт.".format(len(lines) - limit)
        dlg.FindName('tbDetailsHeader').Text = details_header or u"Элементов: {}".format(len(lines))
        dlg.FindName('tbDetails').Text = shown
    else:
        dlg.FindName('bdDetails').Visibility = Visibility.Collapsed

    btn_yes = dlg.FindName('btnYes')
    btn_no = dlg.FindName('btnNo')
    if not ask:
        btn_no.Visibility = Visibility.Collapsed
        btn_yes.Content = u"OK"
        btn_yes.IsDefault = True
        btn_yes.IsCancel = True
    elif input_default is not None:
        btn_yes.Content = u"Сохранить"
        btn_no.Content = u"Отмена"
        btn_yes.IsDefault = True

    def on_yes(s, e):
        result[0] = True
        dlg.Close()

    def on_drag(s, e):
        try:
            dlg.DragMove()
        except Exception:
            pass

    btn_yes.Click += on_yes
    btn_no.Click += lambda s, e: dlg.Close()
    dlg.FindName('HeaderBar').MouseLeftButtonDown += on_drag

    owner = dialog_owner
    if owner is not None and owner.IsVisible:
        dlg.Owner = owner
        dlg.WindowStartupLocation = System.Windows.WindowStartupLocation.CenterOwner
    else:
        dlg.WindowStartupLocation = System.Windows.WindowStartupLocation.CenterScreen
        try:
            WindowInteropHelper(dlg).Owner = revit_window_handle()
        except Exception:
            pass
    if input_default is not None:
        dlg.Loaded += lambda s, e: (tb_input.Focus(), tb_input.SelectAll())
    dlg.ShowDialog()
    return result[0], tb_input.Text


def revit_window_handle():
    """Главное окно Revit — владелец окон кнопок. Process.MainWindowHandle может указать на только что
    закрытое окно (ShowDialog: «Недопустимый дескриптор окна»), поэтому берём окно из UIApplication."""
    try:
        from pyrevit import HOST_APP
        return HOST_APP.uiapp.MainWindowHandle
    except Exception:
        return Process.GetCurrentProcess().MainWindowHandle


def show_message(text, kind='warn', title=None, details=None, ask=False, limit=500, details_header=None):
    """Модальный диалог в стиле аудита. ask=True — кнопки Да/Нет. Возвращает True при «Да»/«OK»."""
    ok, _ = _run_dialog(text, kind, title, details, ask, limit, None, details_header)
    return ok


# ==========================================================
# XAML — ОБЩИЕ РЕСУРСЫ (СТИЛИ ОКНА АУДИТА) И ВЕРХНЯЯ ПАНЕЛЬ
# ==========================================================
XAML_RESOURCES = u"""
        <SolidColorBrush x:Key="Accent" Color="#FFFF7A1A"/>
        <LinearGradientBrush x:Key="AccentGrad" StartPoint="0,0" EndPoint="0,1">
            <GradientStop Color="#FFFF9440" Offset="0"/>
            <GradientStop Color="#FFFF6A00" Offset="1"/>
        </LinearGradientBrush>
        <LinearGradientBrush x:Key="GlassFill" StartPoint="0,0" EndPoint="1,1">
            <GradientStop Color="#E6FFFFFF" Offset="0"/>
            <GradientStop Color="#B8FFFFFF" Offset="1"/>
        </LinearGradientBrush>

        <Style x:Key="ScrollThumbStyle" TargetType="Thumb">
            <Setter Property="SnapsToDevicePixels" Value="True"/>
            <Setter Property="OverridesDefaultStyle" Value="True"/>
            <Setter Property="IsTabStop" Value="False"/>
            <Setter Property="Focusable" Value="False"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Thumb">
                        <Border Name="thumbBd" Background="{StaticResource AccentGrad}" BorderBrush="#80FFFFFF"
                                BorderThickness="1" CornerRadius="5" Opacity="0.72"/>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="thumbBd" Property="Opacity" Value="0.95"/>
                            </Trigger>
                            <Trigger Property="IsDragging" Value="True">
                                <Setter TargetName="thumbBd" Property="Opacity" Value="1.0"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="ScrollPageButtonStyle" TargetType="RepeatButton">
            <Setter Property="SnapsToDevicePixels" Value="True"/>
            <Setter Property="OverridesDefaultStyle" Value="True"/>
            <Setter Property="IsTabStop" Value="False"/>
            <Setter Property="Focusable" Value="False"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="RepeatButton">
                        <Border Background="Transparent"/>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style TargetType="ScrollBar">
            <Setter Property="SnapsToDevicePixels" Value="True"/>
            <Setter Property="OverridesDefaultStyle" Value="True"/>
            <Style.Triggers>
                <Trigger Property="Orientation" Value="Vertical">
                    <Setter Property="Width" Value="10"/>
                    <Setter Property="Height" Value="Auto"/>
                    <Setter Property="Margin" Value="4,2,2,2"/>
                    <Setter Property="Template">
                        <Setter.Value>
                            <ControlTemplate TargetType="ScrollBar">
                                <Grid>
                                    <Border Background="#1FFF7A1A" BorderBrush="#33FF7A1A" BorderThickness="1" CornerRadius="5"/>
                                    <Track Name="PART_Track" IsDirectionReversed="True">
                                        <Track.DecreaseRepeatButton>
                                            <RepeatButton Style="{StaticResource ScrollPageButtonStyle}" Command="ScrollBar.PageUpCommand"/>
                                        </Track.DecreaseRepeatButton>
                                        <Track.Thumb>
                                            <Thumb Style="{StaticResource ScrollThumbStyle}" Margin="1"/>
                                        </Track.Thumb>
                                        <Track.IncreaseRepeatButton>
                                            <RepeatButton Style="{StaticResource ScrollPageButtonStyle}" Command="ScrollBar.PageDownCommand"/>
                                        </Track.IncreaseRepeatButton>
                                    </Track>
                                </Grid>
                            </ControlTemplate>
                        </Setter.Value>
                    </Setter>
                </Trigger>
                <Trigger Property="Orientation" Value="Horizontal">
                    <Setter Property="Width" Value="Auto"/>
                    <Setter Property="Height" Value="10"/>
                    <Setter Property="Margin" Value="2,4,2,2"/>
                    <Setter Property="Template">
                        <Setter.Value>
                            <ControlTemplate TargetType="ScrollBar">
                                <Grid>
                                    <Border Background="#1FFF7A1A" BorderBrush="#33FF7A1A" BorderThickness="1" CornerRadius="5"/>
                                    <Track Name="PART_Track" IsDirectionReversed="False">
                                        <Track.DecreaseRepeatButton>
                                            <RepeatButton Style="{StaticResource ScrollPageButtonStyle}" Command="ScrollBar.PageLeftCommand"/>
                                        </Track.DecreaseRepeatButton>
                                        <Track.Thumb>
                                            <Thumb Style="{StaticResource ScrollThumbStyle}" Margin="1"/>
                                        </Track.Thumb>
                                        <Track.IncreaseRepeatButton>
                                            <RepeatButton Style="{StaticResource ScrollPageButtonStyle}" Command="ScrollBar.PageRightCommand"/>
                                        </Track.IncreaseRepeatButton>
                                    </Track>
                                </Grid>
                            </ControlTemplate>
                        </Setter.Value>
                    </Setter>
                </Trigger>
            </Style.Triggers>
        </Style>

        <Style x:Key="GlassCard" TargetType="Border">
            <Setter Property="Background" Value="{StaticResource GlassFill}"/>
            <Setter Property="BorderBrush" Value="#80FFFFFF"/>
            <Setter Property="BorderThickness" Value="1"/>
            <Setter Property="CornerRadius" Value="20"/>
        </Style>

        <Style x:Key="PrimaryButton" TargetType="Button">
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="Padding" Value="20,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Name="bd" Background="{StaticResource AccentGrad}" CornerRadius="19" BorderThickness="0" Padding="{TemplateBinding Padding}">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="bd" Property="Opacity" Value="0.9"/></Trigger>
                            <Trigger Property="IsEnabled" Value="False"><Setter TargetName="bd" Property="Opacity" Value="0.45"/></Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="GlassButton" TargetType="Button">
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="Padding" Value="18,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="#66FF7A1A" BorderThickness="1" Padding="{TemplateBinding Padding}">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="bd" Property="Background" Value="#FFFFFFFF"/></Trigger>
                            <Trigger Property="IsEnabled" Value="False"><Setter TargetName="bd" Property="Opacity" Value="0.45"/></Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <!-- Круглая кнопка «...» для выбора файла или папки -->
        <Style x:Key="IconButton" TargetType="Button">
            <Setter Property="Foreground" Value="#FFC24E00"/>
            <Setter Property="FontWeight" Value="Bold"/>
            <Setter Property="Width" Value="38"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="#66FF7A1A" BorderThickness="1">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="#FFFFFFFF"/>
                                <Setter TargetName="bd" Property="BorderBrush" Value="#FFFF7A1A"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <!-- Переключатель-«таблетка» (выбор варианта) -->
        <Style x:Key="ModePill" TargetType="RadioButton">
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="MinWidth" Value="110"/>
            <Setter Property="Margin" Value="0,0,8,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="RadioButton">
                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="#66FF7A1A" BorderThickness="1" Padding="18,0">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="#FFFFFFFF"/>
                                <Setter TargetName="bd" Property="BorderBrush" Value="#FFFF7A1A"/>
                            </Trigger>
                            <Trigger Property="IsChecked" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="{StaticResource AccentGrad}"/>
                                <Setter TargetName="bd" Property="BorderBrush" Value="Transparent"/>
                                <Setter Property="Foreground" Value="White"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="PillTextBox" TargetType="TextBox">
            <Setter Property="Height" Value="38"/>
            <Setter Property="FontSize" Value="13"/>
            <Setter Property="Padding" Value="12,0"/>
            <Setter Property="VerticalContentAlignment" Value="Center"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="TextBox">
                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="#66FF7A1A" BorderThickness="1">
                            <ScrollViewer Name="PART_ContentHost" Margin="{TemplateBinding Padding}" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsKeyboardFocused" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="#FFFFFFFF"/>
                                <Setter TargetName="bd" Property="BorderBrush" Value="#FFFF7A1A"/>
                            </Trigger>
                            <Trigger Property="IsEnabled" Value="False">
                                <Setter TargetName="bd" Property="Opacity" Value="0.5"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="PillComboBox" TargetType="ComboBox">
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="FontSize" Value="13"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="ComboBox">
                        <Grid>
                            <ToggleButton Name="ToggleButton" Focusable="False" ClickMode="Press"
                                          IsChecked="{Binding Path=IsDropDownOpen, Mode=TwoWay, RelativeSource={RelativeSource TemplatedParent}}">
                                <ToggleButton.Template>
                                    <ControlTemplate TargetType="ToggleButton">
                                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="#66FF7A1A" BorderThickness="1" Padding="16,0,14,0">
                                            <Grid>
                                                <Grid.ColumnDefinitions>
                                                    <ColumnDefinition Width="Auto"/>
                                                    <ColumnDefinition Width="*"/>
                                                    <ColumnDefinition Width="Auto"/>
                                                </Grid.ColumnDefinitions>
                                                <TextBlock Grid.Column="0" Text="{Binding Path=Tag, RelativeSource={RelativeSource AncestorType=ComboBox}}" Foreground="#FF8C7A6B" FontSize="12" FontWeight="SemiBold" VerticalAlignment="Center" Margin="0,0,8,0"/>
                                                <TextBlock Grid.Column="1" Text="{Binding Path=SelectedItem.Name, RelativeSource={RelativeSource AncestorType=ComboBox}, FallbackValue='Выберите...'}"
                                                           Foreground="#FF2B2118" FontSize="13" FontWeight="Bold" VerticalAlignment="Center" TextTrimming="CharacterEllipsis"/>
                                                <Path Grid.Column="2" Data="M0,0 L5,5 L10,0" Stroke="#FFFF7A1A" StrokeThickness="2" VerticalAlignment="Center" Margin="10,2,0,0"/>
                                            </Grid>
                                        </Border>
                                        <ControlTemplate.Triggers>
                                            <Trigger Property="IsMouseOver" Value="True">
                                                <Setter TargetName="bd" Property="Background" Value="#FFFFFFFF"/>
                                                <Setter TargetName="bd" Property="BorderBrush" Value="#FFFF7A1A"/>
                                            </Trigger>
                                            <Trigger Property="IsChecked" Value="True">
                                                <Setter TargetName="bd" Property="Background" Value="#FFFFF4EB"/>
                                                <Setter TargetName="bd" Property="BorderBrush" Value="#FFFF7A1A"/>
                                            </Trigger>
                                        </ControlTemplate.Triggers>
                                    </ControlTemplate>
                                </ToggleButton.Template>
                            </ToggleButton>
                            <Popup Name="Popup" Placement="Bottom" VerticalOffset="6" IsOpen="{TemplateBinding IsDropDownOpen}"
                                   AllowsTransparency="True" Focusable="False" PopupAnimation="Slide">
                                <Border Background="#FFFFFFFF" BorderBrush="#66FF7A1A" BorderThickness="1" CornerRadius="16" Padding="6"
                                        MinWidth="{TemplateBinding ActualWidth}" MaxHeight="320">
                                    <ScrollViewer SnapsToDevicePixels="True" VerticalScrollBarVisibility="Auto">
                                        <StackPanel IsItemsHost="True" KeyboardNavigation.DirectionalNavigation="Contained"/>
                                    </ScrollViewer>
                                </Border>
                            </Popup>
                        </Grid>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
            <Setter Property="ItemContainerStyle">
                <Setter.Value>
                    <Style TargetType="ComboBoxItem">
                        <Setter Property="Padding" Value="12,8"/>
                        <Setter Property="Cursor" Value="Hand"/>
                        <Setter Property="Template">
                            <Setter.Value>
                                <ControlTemplate TargetType="ComboBoxItem">
                                    <Border Name="ibd" Background="Transparent" CornerRadius="10" Padding="{TemplateBinding Padding}">
                                        <TextBlock Text="{Binding Name}" FontWeight="SemiBold" FontSize="13" Foreground="#FF2B2118"/>
                                    </Border>
                                    <ControlTemplate.Triggers>
                                        <Trigger Property="IsHighlighted" Value="True">
                                            <Setter TargetName="ibd" Property="Background" Value="#1FFF7A1A"/>
                                        </Trigger>
                                        <Trigger Property="IsSelected" Value="True">
                                            <Setter TargetName="ibd" Property="Background" Value="#33FF7A1A"/>
                                        </Trigger>
                                    </ControlTemplate.Triggers>
                                </ControlTemplate>
                            </Setter.Value>
                        </Setter>
                    </Style>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="NavItem" TargetType="RadioButton">
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="FontSize" Value="14"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Height" Value="44"/>
            <Setter Property="Margin" Value="0,0,0,6"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="RadioButton">
                        <Border Name="bd" CornerRadius="14" Background="Transparent" Padding="14,0">
                            <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
                                <Ellipse Name="dot" Width="8" Height="8" Fill="{StaticResource Accent}" Margin="0,0,10,0"/>
                                <ContentPresenter VerticalAlignment="Center"/>
                            </StackPanel>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="#14FF7A1A"/>
                            </Trigger>
                            <Trigger Property="IsChecked" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="{StaticResource AccentGrad}"/>
                                <Setter TargetName="dot" Property="Fill" Value="White"/>
                                <Setter Property="Foreground" Value="White"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="PillCheck" TargetType="CheckBox">
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="CheckBox">
                        <StackPanel Orientation="Horizontal" Background="Transparent">
                            <Border Name="box" Width="20" Height="20" CornerRadius="6" Background="#CCFFFFFF" BorderBrush="#66FF7A1A" BorderThickness="1" VerticalAlignment="Center">
                                <Path Name="mark" Data="M4,10 L8,14 L16,6" Stroke="White" StrokeThickness="2.2" Visibility="Hidden"/>
                            </Border>
                            <ContentPresenter VerticalAlignment="Center" Margin="10,0,0,0"/>
                        </StackPanel>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsChecked" Value="True">
                                <Setter TargetName="box" Property="Background" Value="{StaticResource AccentGrad}"/>
                                <Setter TargetName="box" Property="BorderBrush" Value="Transparent"/>
                                <Setter TargetName="mark" Property="Visibility" Value="Visible"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <!-- Поля окон армирования: подпись строки, подпись в строке, заголовок поля, подсказка -->
        <Style x:Key="FieldLabel" TargetType="TextBlock">
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="VerticalAlignment" Value="Center"/>
            <Setter Property="TextWrapping" Value="Wrap"/>
            <Setter Property="Margin" Value="4,0,12,0"/>
        </Style>
        <Style x:Key="Inline" TargetType="TextBlock">
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="VerticalAlignment" Value="Center"/>
            <Setter Property="Margin" Value="8,0,6,0"/>
        </Style>
        <Style x:Key="Caption" TargetType="TextBlock">
            <Setter Property="FontSize" Value="11"/>
            <Setter Property="FontWeight" Value="Bold"/>
            <Setter Property="Foreground" Value="#FF8C7A6B"/>
            <Setter Property="Margin" Value="4,0,0,4"/>
        </Style>
        <Style x:Key="Hint" TargetType="TextBlock">
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="Foreground" Value="#FF8C7A6B"/>
            <Setter Property="TextWrapping" Value="Wrap"/>
            <Setter Property="Margin" Value="4,6,0,0"/>
        </Style>
"""

# Верхняя панель с кнопками-«точками»: вставляется в XAML окна вместо __TITLEBAR__
XAML_TITLEBAR = u"""                <Grid Name="TitleBar" Grid.Row="0" Height="46" Background="Transparent">
                    <StackPanel Orientation="Horizontal" VerticalAlignment="Center" Margin="24,0,0,0">
                        <Button Name="btnCloseDot" Width="14" Height="14" Background="#FFFF5F57" Margin="0,0,8,0" Cursor="Hand" ToolTip="Закрыть">
                            <Button.Template>
                                <ControlTemplate TargetType="Button">
                                    <Grid Background="Transparent">
                                        <Ellipse Fill="{TemplateBinding Background}"/>
                                        <Path Name="icon" Data="M4,4 L10,10 M10,4 L4,10" Stroke="#66000000" StrokeThickness="1.5" Visibility="Hidden"/>
                                    </Grid>
                                    <ControlTemplate.Triggers>
                                        <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="icon" Property="Visibility" Value="Visible"/></Trigger>
                                    </ControlTemplate.Triggers>
                                </ControlTemplate>
                            </Button.Template>
                        </Button>
                        <Button Name="btnMinDot" Width="14" Height="14" Background="#FFFEBC2E" Margin="0,0,8,0" Cursor="Hand" ToolTip="Свернуть">
                            <Button.Template>
                                <ControlTemplate TargetType="Button">
                                    <Grid Background="Transparent">
                                        <Ellipse Fill="{TemplateBinding Background}"/>
                                        <Path Name="icon" Data="M3.5,7 L10.5,7" Stroke="#66000000" StrokeThickness="1.5" Visibility="Hidden"/>
                                    </Grid>
                                    <ControlTemplate.Triggers>
                                        <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="icon" Property="Visibility" Value="Visible"/></Trigger>
                                    </ControlTemplate.Triggers>
                                </ControlTemplate>
                            </Button.Template>
                        </Button>
                        <Button Name="btnMaxDot" Width="14" Height="14" Background="#FF28C840" Margin="0,0,8,0" Cursor="Hand" ToolTip="Развернуть">
                            <Button.Template>
                                <ControlTemplate TargetType="Button">
                                    <Grid Background="Transparent">
                                        <Ellipse Fill="{TemplateBinding Background}"/>
                                        <Path Name="icon" Data="M3.5,3.5 L7.5,3.5 L3.5,7.5 Z M10.5,10.5 L6.5,10.5 L10.5,6.5 Z" Fill="#66000000" Visibility="Hidden"/>
                                    </Grid>
                                    <ControlTemplate.Triggers>
                                        <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="icon" Property="Visibility" Value="Visible"/></Trigger>
                                    </ControlTemplate.Triggers>
                                </ControlTemplate>
                            </Button.Template>
                        </Button>
                    </StackPanel>
                </Grid>
"""


# ==========================================================
# ОКНО КНОПКИ КЖ: ЗАГРУЗКА XAML, ПЕРЕТАСКИВАНИЕ, НАВИГАЦИЯ
# ==========================================================
class Option(object):
    """Пункт выпадающего списка (PillComboBox показывает свойство Name)."""
    def __init__(self, name, value):
        self.Name = name
        self.Value = value

    def ToString(self):
        return self.Name

    def __str__(self):
        return self.Name


def select_option(combo, value):
    for opt in combo.ItemsSource:
        if opt.Value == value:
            combo.SelectedItem = opt
            return
    combo.SelectedIndex = 0


def selected_value(combo, default=None):
    opt = combo.SelectedItem
    return opt.Value if opt is not None else default


def parse_number(text, default=None):
    """Число из поля ввода: пусто или ошибка -> default. Запятая допускается как разделитель."""
    try:
        value = (text or u"").strip().replace(u",", u".")
        return float(value) if value else default
    except Exception:
        return default


def number_text(value):
    """Число для поля ввода: 50.0 -> «50», 12.5 -> «12.5», None -> пусто."""
    if value is None:
        return u""
    if abs(value - round(value)) < 1e-6:
        return u"{}".format(int(round(value)))
    return u"{}".format(value)


def field_row(label, name, width=110):
    """XAML строки «подпись — поле ввода» (стиль FieldLabel и PillTextBox)."""
    return u"""<Grid Margin="0,0,0,8">
    <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="{w}"/>
    </Grid.ColumnDefinitions>
    <TextBlock Text="{label}" Style="{{StaticResource FieldLabel}}"/>
    <TextBox Name="{name}" Grid.Column="1" Style="{{StaticResource PillTextBox}}"/>
</Grid>""".format(w=width, label=label, name=name)


class KzhWindow(object):
    """Модальное окно кнопки КЖ. XAML содержит метки __RESOURCES__ и __TITLEBAR__.
    nav_pages — пары (имя RadioButton меню, имя страницы)."""

    def __init__(self, xaml, control_names, nav_pages=()):
        global dialog_owner
        self.window = XamlReader.Parse(xaml.replace('__RESOURCES__', XAML_RESOURCES)
                                       .replace('__TITLEBAR__', XAML_TITLEBAR))
        dialog_owner = self.window
        # Окно не больше рабочей области экрана (ноутбуки)
        area = System.Windows.SystemParameters.WorkArea
        self.window.Width = min(self.window.Width, area.Width - 20)
        self.window.Height = min(self.window.Height, area.Height - 20)
        self.window.MinWidth = min(self.window.MinWidth, self.window.Width)
        self.window.MinHeight = min(self.window.MinHeight, self.window.Height)
        for name in control_names:
            setattr(self, name, self.window.FindName(name))
        self.TitleBar = self.window.FindName('TitleBar')
        self.TitleBar.MouseLeftButtonDown += self._on_drag
        self.window.FindName('btnCloseDot').Click += lambda s, e: self.window.Close()
        self.window.FindName('btnMinDot').Click += self._on_minimize
        self.window.FindName('btnMaxDot').Click += self._on_maximize
        self._nav = [(self.window.FindName(n), self.window.FindName(p)) for n, p in nav_pages]
        for nav, _ in self._nav:
            nav.Checked += self._on_nav

    def _on_drag(self, sender, args):
        try:
            node = args.OriginalSource
            while node is not None and node != self.TitleBar:
                if isinstance(node, Button):
                    return
                node = VisualTreeHelper.GetParent(node)
            self.window.DragMove()
        except Exception:
            pass

    def _on_minimize(self, sender, args):
        self.window.WindowState = WindowState.Minimized

    def _on_maximize(self, sender, args):
        self.window.WindowState = (WindowState.Normal if self.window.WindowState == WindowState.Maximized
                                   else WindowState.Maximized)

    def _on_nav(self, sender, args):
        for nav, page in self._nav:
            if page is not None:
                page.Visibility = Visibility.Visible if nav.IsChecked else Visibility.Collapsed

    def show_modal(self):
        try:
            WindowInteropHelper(self.window).Owner = revit_window_handle()
        except Exception:
            pass
        self.window.ShowDialog()
